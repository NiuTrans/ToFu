//! Logical event payload durability, ownership, retention and physical budgets.
use super::*;
use crate::authority_gc::AuthorityGarbageCollectionBudget;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use std::{path::Path, sync::Arc};

fn large_request() -> AppendRequest {
    AppendRequest {
        task_id: "blob-task".to_owned(),
        application_sequence: 19,
        event_type: "messages_snapshot".to_owned(),
        created_at_ms: 1234,
        payload_json: serde_json::to_vec(
            &json!({"type":"messages_snapshot", "text":"x".repeat(300 * 1024)}),
        )
        .unwrap(),
    }
}
fn write(db: &mut AuthorityDatabase, item: &AppendRequest) -> io::Result<()> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    let result: Value = serde_json::from_slice(&append(db, &mut tx, item)?).unwrap();
    if result["inserted"] == true {
        db.commit(tx)?;
    }
    Ok(())
}
fn rows(db: &AuthorityDatabase, owner: u64) -> io::Result<Value> {
    let mut tx = db.begin_with_identity_claim_scopes(7, owner)?;
    let raw = list(
        db,
        &mut tx,
        &ListRequest {
            task_id: "blob-task".to_owned(),
            after_sequence: None,
            limit: 10,
            types: vec!["messages_snapshot".to_owned()],
            type_prefixes: vec![],
            payload_types: None,
        },
    )?;
    Ok(serde_json::from_slice(&raw).unwrap())
}

#[test]
fn event_time_spine_omits_blob_materialization_but_full_replay_still_verifies_it() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let item = large_request();
    write(&mut db, &item).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    // Inject unavailable payload storage only in this private transaction.
    // Metadata reads must not fetch excluded content; full replay must fail.
    db.entity_delete(&mut tx, payload_document_key(7, 11, &item.task_id, 19).unwrap()).unwrap();
    let selected = ListRequest {
        task_id: item.task_id.clone(), after_sequence: None, limit: 10,
        types: vec![], type_prefixes: vec![], payload_types: Some(vec![]),
    };
    let spine: Value = serde_json::from_slice(&list(&db, &mut tx, &selected).unwrap()).unwrap();
    assert_eq!(spine, json!([{"sequence": 19, "created_at_ms": 1234,
                            "event": {"type": "messages_snapshot"}}]));
    let end: Value = serde_json::from_slice(
        &latest(&db, &mut tx, &item.task_id, true).unwrap().unwrap()).unwrap();
    assert_eq!(end, spine[0]);
    let full = ListRequest { payload_types: None, ..selected };
    assert!(list(&db, &mut tx, &full).unwrap_err().to_string().contains("missing"));
    assert!(latest(&db, &mut tx, &item.task_id, false).unwrap_err().to_string().contains("missing"));
    drop(tx);
    // The injected failure never changes durable payload state.
    assert_eq!(rows(&db, 11).unwrap()[0]["event"]["text"].as_str().unwrap().len(), 300 * 1024);
}
#[test]
fn large_event_payload_survives_gc_reopen_and_prune() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let item = large_request();
    write(&mut db, &item).unwrap();
    db.collect_garbage(AuthorityGarbageCollectionBudget::conservative())
        .unwrap();
    drop(db);
    let mut db = AuthorityDatabase::open(dir.path()).unwrap();
    let expected: Value = serde_json::from_slice(&item.payload_json).unwrap();
    assert_eq!(rows(&db, 11).unwrap()[0]["event"], expected);
    assert_eq!(rows(&db, 12).unwrap(), json!([]));
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let latest: Value =
        serde_json::from_slice(&latest(&db, &mut tx, &item.task_id, false).unwrap().unwrap()).unwrap();
    assert_eq!(latest["event"], expected);
    let physical = db
        .stream_read_in_transaction(&mut tx, &stream_key(7, 11, &item.task_id).unwrap(), 1, 1)
        .unwrap();
    assert_eq!(physical.events[0].event.payload.len(), 16);
    let key = payload_document_key(7, 11, &item.task_id, 19).unwrap();
    assert!(crate::versioned_document::stored_blob_reference(
        &db.entity_get(&mut tx, &key).unwrap().unwrap()
    )
    .unwrap()
    .is_some());
    let replay: Value = serde_json::from_slice(&append(&db, &mut tx, &item).unwrap()).unwrap();
    assert_eq!(replay["inserted"], false);
    prune(
        &db,
        &mut tx,
        &PruneRequest {
            created_before_ms: 2000,
            limit: 1,
            retention_class: RetentionClass::Structural,
            now_unix_ms: 10_000,
        },
        &|| false,
    )
    .unwrap();
    db.commit(tx).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(db.entity_get(&mut tx, &key).unwrap().is_none());
    assert_eq!(rows(&db, 11).unwrap(), json!([]));
    drop(tx);
    db.collect_garbage(AuthorityGarbageCollectionBudget::conservative())
        .unwrap();
    drop(db);
    let db = AuthorityDatabase::open(dir.path()).unwrap();
    assert_eq!(rows(&db, 11).unwrap(), json!([]));
}

fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/events")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let db = AuthorityDatabase::initialize_with_vfs(Path::new("/events"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, db)
}
#[test]
fn large_event_payload_commit_faults_recover_atomically() {
    let (vfs, mut db) = prepared();
    let item = large_request();
    write(&mut db, &item).unwrap();
    let trace = vfs.trace().unwrap();
    let expected: Value = serde_json::from_slice(&item.payload_json).unwrap();
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
        if operation == &Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        if matches!(
            operation,
            Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let (vfs, mut db) = prepared();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = write(&mut db, &item).is_ok();
            drop(db);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut db = AuthorityDatabase::open_with_vfs(Path::new("/events"), vfs).unwrap();
            let restored = rows(&db, 11).unwrap();
            let exists = !restored.as_array().unwrap().is_empty();
            assert!(
                !acknowledged || exists,
                "lost acknowledged event {index} {action:?}"
            );
            if exists {
                assert_eq!(restored[0]["event"], expected);
            }
            write(&mut db, &item).unwrap();
            assert_eq!(rows(&db, 11).unwrap()[0]["event"], expected);
        }
    }
}

#[test]
fn large_event_payload_rejects_invalid_json_and_corrupt_identity() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let mut item = large_request();
    item.payload_json = b"[invalid".to_vec();
    assert_eq!(write(&mut db,&item).unwrap_err().kind(),io::ErrorKind::InvalidData);
    assert_eq!(rows(&db,11).unwrap(),json!([]));
    let item = large_request();
    write(&mut db, &item).unwrap();
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let entry = IndexEntry::decode(
        &db.entity_get(&mut tx, &index_key(7, 11, &item.task_id, 19).unwrap())
            .unwrap()
            .unwrap(),
    )
    .unwrap();
    let mut reference = PAYLOAD_REFERENCE_MAGIC.to_vec();
    reference.extend_from_slice(&20u64.to_be_bytes());
    let event = StreamEvent::new(1234, "messages_snapshot", reference).unwrap();
    assert_eq!(
        materialize_event_payload(&db, &mut tx, &item.task_id, entry, &event)
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidData
    );
    db.entity_delete(
        &mut tx,
        payload_document_key(7, 11, &item.task_id, 19).unwrap(),
    )
    .unwrap();
    db.commit(tx).unwrap();
    assert_eq!(
        rows(&db, 11).unwrap_err().kind(),
        io::ErrorKind::InvalidData
    );
}

fn prune_large(db: &mut AuthorityDatabase) -> io::Result<()> {
    let mut tx = db.begin_with_identity_claim_scopes(7, 11)?;
    prune(
        db,
        &mut tx,
        &PruneRequest {
            created_before_ms: 2000,
            limit: 1,
            retention_class: RetentionClass::Structural,
            now_unix_ms: 10_000,
        },
        &|| false,
    )?;
    db.commit(tx)?;
    Ok(())
}
#[test]
fn large_event_payload_prune_faults_keep_document_and_indexes_together() {
    let item = large_request();
    let (vfs, mut db) = prepared();
    write(&mut db, &item).unwrap();
    vfs.arm_fault(None).unwrap();
    prune_large(&mut db).unwrap();
    let trace = vfs.trace().unwrap();
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::StorageFull)];
        if operation == &Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        if matches!(
            operation,
            Operation::SyncAll | Operation::SyncData | Operation::SyncDirectory
        ) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let (vfs, mut db) = prepared();
            write(&mut db, &item).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = prune_large(&mut db).is_ok();
            drop(db);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let db = AuthorityDatabase::open_with_vfs(Path::new("/events"), vfs).unwrap();
            let remains = !rows(&db, 11).unwrap().as_array().unwrap().is_empty();
            assert!(
                !acknowledged || !remains,
                "lost acknowledged prune {index} {action:?}"
            );
            let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
            let key = payload_document_key(7, 11, &item.task_id, 19).unwrap();
            assert_eq!(
                db.entity_get(&mut tx, &key).unwrap().is_some(),
                remains,
                "orphan payload after prune {index} {action:?}"
            );
        }
    }
}

#[test]
fn inspector_summary_reads_all_roots_and_child_pages_after_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let roots: Vec<String> = (0..129).map(|i| format!("inspector-demand-{i:04}")).collect();
    let children: Vec<String> = (0..1101).map(|i| format!("{}#agent:{i:04}", roots[0])).collect();
    let mut identities = roots.clone();
    identities.extend(children.clone());
    identities.sort();
    for chunk in identities.chunks(256) {
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let events: Vec<AppendRequest> = chunk.iter().map(|task_id| AppendRequest {
            task_id: task_id.clone(), application_sequence: 0,
            event_type: "messages_snapshot".to_owned(), created_at_ms: 1234,
            payload_json: br#"{"type":"messages_snapshot","kind":"request"}"#.to_vec(),
        }).collect();
        append_batch(&db, &mut tx, &events).unwrap();
        db.commit(tx).unwrap();
    }
    drop(db);
    let db = AuthorityDatabase::open(dir.path()).unwrap();
    let mut requested = roots.clone();
    requested.extend([roots[0].clone(), children[0].clone()]);
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let result: Value = serde_json::from_slice(&inspector_summary(&db, &mut tx, &requested).unwrap()).unwrap();
    let records = result["records"].as_array().unwrap();
    assert_eq!(records.len(), identities.len());
    for (record, identity) in records.iter().zip(&identities) {
        assert_eq!(record["task_id"], identity.as_str());
        assert_eq!(record["request_count"], 1);
        assert_eq!(record["event_count"], 1);
        assert_eq!(record["first_event_at_ms"], 1234);
    }
    let mut foreign = db.begin_with_identity_claim_scopes(7, 12).unwrap();
    let hidden: Value = serde_json::from_slice(&inspector_summary(&db, &mut foreign, &requested).unwrap()).unwrap();
    assert_eq!(hidden["records"], json!([]));
}

#[test]
fn complete_type_catalog_merges_sparse_events_and_summarizes_after_reopen() {
    let dir = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(dir.path()).unwrap();
    let task_id = "many-event-types";
    let mut events: Vec<AppendRequest> = (0..2161).map(|i| AppendRequest {
        task_id: task_id.to_owned(), application_sequence: i * 7 + 11,
        event_type: format!("endpoint_{:04}", 2160 - i), created_at_ms: 1234,
        payload_json: serde_json::to_vec(&json!({"type": format!("endpoint_{:04}", 2160 - i)})).unwrap(),
    }).collect();
    events.push(AppendRequest {
        task_id: task_id.to_owned(), application_sequence: 50000,
        event_type: events[0].event_type.clone(), created_at_ms: 1234,
        payload_json: events[0].payload_json.clone(),
    });
    for chunk in events.chunks(256) {
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_batch(&db, &mut tx, chunk).unwrap();
        db.commit(tx).unwrap();
    }
    drop(db);
    let db = AuthorityDatabase::open(dir.path()).unwrap();
    let mut seen = Vec::new();
    let mut after_sequence = None;
    loop {
        let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
        let request = ListRequest {
            task_id: task_id.to_owned(), after_sequence, limit: 197,
            types: vec![events[0].event_type.clone()],
            type_prefixes: vec!["endpoint_".to_owned(), "endpoint_00".to_owned()],
            payload_types: Some(vec![]),
        };
        let page: Value = serde_json::from_slice(&list(&db, &mut tx, &request).unwrap()).unwrap();
        let rows = page.as_array().unwrap();
        if rows.is_empty() { break; }
        assert!(rows.len() <= 197);
        for row in rows {
            seen.push((row["sequence"].as_u64().unwrap(), row["event"]["type"].as_str().unwrap().to_owned()));
        }
        after_sequence = Some(seen.last().unwrap().0);
    }
    assert_eq!(seen, events.iter().map(|event| (event.application_sequence, event.event_type.clone())).collect::<Vec<_>>());
    let mut tx = db.begin_with_identity_claim_scopes(7, 11).unwrap();
    let summary: Value = serde_json::from_slice(&inspector_summary(&db, &mut tx, &[task_id.to_owned()]).unwrap()).unwrap();
    assert_eq!(summary["records"][0]["event_count"], events.len());
    assert_eq!(summary["records"][0]["first_event_at_ms"], 1234);
}
