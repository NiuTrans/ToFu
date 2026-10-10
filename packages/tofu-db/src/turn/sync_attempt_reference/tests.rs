//! Same-owner canonical replay, retention protection and bounded admission.
use super::*;
use super::super::*;
use std::{path::Path, sync::Arc};
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

fn seed(database: &mut AuthorityDatabase) {
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(database, &mut tx, &AppendSettledRequest {
        conversation_id: "ref-conversation".into(), actor: "assistant".into(),
        status: "completed".into(), projection_json: b"{\"content\":\"\"}".to_vec(),
        projection_timestamp: None, settlement_json: b"{}".to_vec(), lane_id: "main".into(),
        command_id: "ref-seed".into(), kind: "reply".into(), run_id: String::new(),
        turn_id: "ref-turn".into(), attempt_id: Some("ref-attempt".into()),
        created_at_ms: 1_000, committed_at_ms: 1_000,
        defaults: crate::conversation_header::TurnDefaults {
            allow_create: true, title: "Reference".into(), settings_json: b"{}".to_vec(),
            created_at_ms: 1_000,
        },
    }).unwrap();
    database.commit(tx).unwrap();
}

fn append(database: &mut AuthorityDatabase) -> (Value, u64) {
    let mut tx = database.begin(7, 11).unwrap();
    let canonical = prepare_append(database, &mut tx);
    let cursor = sync_head(database, &mut tx, "ref-conversation").unwrap();
    database.commit(tx).unwrap();
    (canonical, cursor)
}

fn prepare_append(database: &AuthorityDatabase, tx: &mut AuthorityTransaction) -> Value {
    append_attempt_event(database, tx, AttemptEventAppend {
        conversation_id: "ref-conversation", turn_id: "ref-turn", attempt_id: "ref-attempt",
        projection_revision: 1, event_type: "projection_updated",
        payload: json!({"status":"running", "projectionPatch":{
            "version":1,"baseRevision":0,"targetRevision":1,"operations":[
                {"op":"set","path":["content"],"value":"汉😀\\\"".repeat(32_768)}]}}),
        occurred_at_ms: 2_000, publish_conversation_sync: true,
    }).unwrap()
}

fn expected(canonical: &Value, cursor: u64) -> Vec<u8> {
    serde_json::to_vec(&conversation_sync_event("ref-conversation", cursor,
        "attempt.event", 2_000, json!({"event":canonical}),
        Some("ref-turn"), Some("ref-attempt"))).unwrap()
}

#[test]
fn large_event_is_stored_once_and_replay_export_preserve_the_former_envelope() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let (canonical, cursor) = append(&mut database);
    let mut tx = database.begin(7, 11).unwrap();
    let key = conversation_sync_event_key(&tx, "ref-conversation", cursor).unwrap();
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    assert!(stored.len() < 1024, "journal duplicated the large source payload");
    assert!(crate::versioned_document::materialize_stored_document(
        &database, 7, 11, &stored, SYNC_DOCUMENT_IDENTITY).is_err(),
        "older readers must reject the reference representation");
    let (_, public) = materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).unwrap();
    assert_eq!(public, expected(&canonical, cursor));
    let response: Value = serde_json::from_slice(&sync_changes(&database, &mut tx,
        "ref-conversation", cursor - 1, 100, &mut || false).unwrap().unwrap()).unwrap();
    assert_eq!(response["events"][0]["payload"]["event"], canonical);
    let exported = sync_event_migration::execute(&mut database, 7, 11,
        "migration.conversation.sync.event.get", json!({
            "tenant_id":7,"owner_user_id":11,"conversation_id":"ref-conversation",
            "sync_sequence":cursor})).unwrap();
    let expected_hex: String = expected(&canonical, cursor).iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(exported["event_json_hex"], expected_hex);
    let scan = sync_event_migration::execute(&mut database, 7, 11,
        "migration.conversation.sync.event.scan", json!({
            "tenant_id":7,"owner_user_id":11,"cursor":null})).unwrap();
    assert!(scan["rows"].as_array().unwrap().iter().any(|row|
        row["conversation_id"] == "ref-conversation" && row["sync_sequence"] == cursor));
    drop(tx); drop(database);
    let database = AuthorityDatabase::open(directory.path()).unwrap();
    let mut tx = database.begin(7, 11).unwrap();
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    assert_eq!(materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).unwrap().1, expected(&canonical, cursor));
}

#[test]
fn missing_source_protection_and_cross_owner_aliases_fail_closed() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let (canonical, cursor) = append(&mut database);
    let mut tx = database.begin(7, 11).unwrap();
    let key = conversation_sync_event_key(&tx, "ref-conversation", cursor).unwrap();
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    let source_sequence = canonical["seq"].as_u64().unwrap();
    let protection = attempt_sync_reference_key(&tx, "ref-attempt", "ref-conversation", cursor).unwrap();
    database.entity_delete(&mut tx, protection.clone()).unwrap();
    assert!(materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).is_err());
    drop(tx);
    let mut other = database.begin(7, 12).unwrap();
    let other_key = conversation_sync_event_key(&other, "ref-conversation", cursor).unwrap();
    assert!(materialize(&database, &mut other, &other_key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).is_err());
    drop(other);
    let mut tx = database.begin(7, 11).unwrap();
    let source = attempt_event_key(&tx, "ref-attempt", source_sequence).unwrap();
    database.entity_delete(&mut tx, source).unwrap();
    assert!(materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).is_err());
}

#[test]
fn retention_protects_the_source_then_old_snapshot_survives_both_prunes() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let (canonical, cursor) = append(&mut database);
    let mut old = database.begin(7, 11).unwrap();
    let key = conversation_sync_event_key(&old, "ref-conversation", cursor).unwrap();
    let stored = database.entity_get(&mut old, &key).unwrap().unwrap();
    let source = attempt_event_key(&old, "ref-attempt", canonical["seq"].as_u64().unwrap()).unwrap();
    let prune = EventPruneRequest { settled_before_ms: 10_000, max_attempts: 8, max_rows: 256 };
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let protected: Value = serde_json::from_slice(&events_prune(&database, &mut tx, &prune).unwrap()).unwrap();
    assert_eq!(protected["deleted_rows"], 0);
    drop(tx);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(database.entity_get(&mut tx, &source).unwrap().is_some());
    sync_prune(&database, &mut tx, &SyncPruneRequest { created_before_ms: 10_000, max_rows: 256 }).unwrap();
    database.commit(tx).unwrap();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    events_prune(&database, &mut tx, &prune).unwrap();
    database.commit(tx).unwrap();
    let mut tx = database.begin(7, 11).unwrap();
    assert!(database.entity_get(&mut tx, &source).unwrap().is_none());
    assert_eq!(materialize(&database, &mut old, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).unwrap().1, expected(&canonical, cursor));
}

#[test]
fn smaller_locator_preserves_the_full_envelope_admission_limit() {
    let envelope = conversation_sync_event("ref-conversation", 1, "attempt.event",
        2_000, json!({"event":{"seq":1}}), Some("ref-turn"), Some("ref-attempt"));
    let overhead = serde_json::to_vec(&envelope).unwrap().len()
        - serde_json::to_vec(&envelope["payload"]["event"]).unwrap().len();
    assert!(encode(&envelope, MAX_TRANSACTION_IR_LITERAL_BYTES - overhead).is_ok());
    let error = encode(&envelope, MAX_TRANSACTION_IR_LITERAL_BYTES - overhead + 1).unwrap_err();
    assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
}


#[test]
fn legacy_bytes_and_reference_bound_remain_exact() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let (canonical, cursor) = append(&mut database);
    let mut tx = database.begin(7, 11).unwrap();
    let key = conversation_sync_event_key(&tx, "ref-conversation", cursor).unwrap();
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    let public = expected(&canonical, cursor);
    assert!(materialize(&database, &mut tx, &key, &stored, public.len() - 1, true).is_err());
    let legacy = [b" ".as_slice(), public.as_slice(), b"\n".as_slice()].concat();
    crate::versioned_document::put(&database, &mut tx,
        crate::versioned_document::PutRequest {
            key: key.clone(), namespace: SYNC_DOCUMENT_IDENTITY.into(),
            logical_key: format!("ref-conversation\u{1f}{cursor:020}"),
            value_json: legacy.clone(), expected_version: Some(1), updated_at_ms: 3_000,
        }).unwrap();
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    assert_eq!(materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).unwrap().1, legacy);
}

#[test]
fn changed_source_version_and_protection_sequence_fail_closed() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let (canonical, cursor) = append(&mut database);
    let mut tx = database.begin(7, 11).unwrap();
    let key = conversation_sync_event_key(&tx, "ref-conversation", cursor).unwrap();
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    let sequence = canonical["seq"].as_u64().unwrap();
    let source = attempt_event_key(&tx, "ref-attempt", sequence).unwrap();
    crate::versioned_document::put(&database, &mut tx, crate::versioned_document::PutRequest {
        key: source, namespace: ATTEMPT_EVENT_DOCUMENT_IDENTITY.into(),
        logical_key: format!("ref-attempt\u{1f}{sequence:020}"),
        value_json: serde_json::to_vec(&canonical).unwrap(), expected_version: Some(1),
        updated_at_ms: 3_000,
    }).unwrap();
    assert!(materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).is_err());
    drop(tx);
    let mut tx = database.begin(7, 11).unwrap();
    let protection = attempt_sync_reference_key(&tx, "ref-attempt", "ref-conversation", cursor).unwrap();
    database.entity_put(&mut tx, protection, (sequence + 1).to_le_bytes().to_vec()).unwrap();
    assert!(materialize(&database, &mut tx, &key, &stored,
        MAX_TRANSACTION_IR_LITERAL_BYTES, true).is_err());
}

fn fault_fixture() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    database.set_write_overlay_for_test(true);
    seed(&mut database);
    (vfs, database)
}

#[test]
fn source_reference_and_cursor_are_atomic_at_every_commit_io_fault() {
    let (vfs, mut database) = fault_fixture();
    let mut tx = database.begin(7, 11).unwrap();
    prepare_append(&database, &mut tx);
    vfs.arm_fault(None).unwrap();
    database.commit(tx).unwrap();
    let trace = vfs.trace().unwrap();
    assert!(!trace.is_empty());
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        if matches!(operation, Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) {
            actions.push(FaultAction::DropSync);
        }
        for action in actions {
            let (vfs, mut database) = fault_fixture();
            let mut tx = database.begin(7, 11).unwrap();
            let before = sync_head(&database, &mut tx, "ref-conversation").unwrap();
            let canonical = prepare_append(&database, &mut tx);
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let acknowledged = database.commit(tx).is_ok();
            let durable = Arc::new(vfs.fork_durable().unwrap());
            let database = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
            let mut tx = database.begin(7, 11).unwrap();
            let head = sync_head(&database, &mut tx, "ref-conversation").unwrap();
            assert!(head == before || head == before + 1, "fault {index} {action:?}");
            let source = attempt_event_key(&tx, "ref-attempt", canonical["seq"].as_u64().unwrap()).unwrap();
            assert_eq!(database.entity_get(&mut tx, &source).unwrap().is_some(), head > before);
            let rows: Value = serde_json::from_slice(&sync_changes(&database, &mut tx,
                "ref-conversation", before, 100, &mut || false).unwrap().unwrap()).unwrap();
            assert_eq!(rows["events"].as_array().unwrap().len(), usize::from(head > before));
            if head > before { assert_eq!(rows["events"][0]["payload"]["event"], canonical); }
            assert!(!acknowledged || head > before, "lost acknowledged event: {index} {action:?}");
        }
    }
}
