use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use std::{path::Path, sync::Arc};

fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/turns")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database =
        AuthorityDatabase::initialize_with_vfs(Path::new("/turns"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, database)
}

fn create_conversation(database: &mut AuthorityDatabase, conversation_id: &str) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    crate::conversation_header::create(
        database,
        &mut transaction,
        &crate::conversation_header::CreateRequest {
            conversation_id: conversation_id.to_owned(),
            title: "Conversation".to_owned(),
            settings_json: b"{}".to_vec(),
            created_at_ms: 500,
            updated_at_ms: 500,
            committed_at_ms: 500,
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
}

fn conversation_header(database: &AuthorityDatabase, conversation_id: &str) -> Value {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let raw = crate::conversation_header::get(
        database,
        &mut transaction,
        conversation_id,
        false,
        0,
        None,
        &mut || false,
    )
    .unwrap()
    .expect("conversation header");
    serde_json::from_slice(&raw).unwrap()
}

fn snapshot(
    turn_id: &str,
    conversation_id: &str,
    lane_id: &str,
    ordinal: u64,
    updated_at: u64,
) -> Value {
    json!({
        "turn_id": turn_id,
        "conversation_id": conversation_id,
        "presentation_id": format!("present-{turn_id}"),
        "lane_id": lane_id,
        "parent_turn_id": Value::Null,
        "ordinal": ordinal,
        "actor": "user",
        "kind": "reply",
        "run_id": format!("run-{turn_id}"),
        "status": "completed",
        "current_attempt_id": Value::Null,
        "projection": {"timestamp": 6_000 + ordinal, "content": format!("body-{turn_id}")},
        "projection_revision": 1,
        "settlement": {"settled": true, "turn": turn_id},
        "created_at": 1_000 + ordinal,
        "updated_at": updated_at,
    })
}

fn import(database: &mut AuthorityDatabase, document: Value) -> io::Result<Value> {
    execute(database, 7, 11, "migration.turn.restore", document)
}

fn point(database: &mut AuthorityDatabase, conversation_id: &str, turn_id: &str) -> Value {
    execute(
        database,
        7,
        11,
        "migration.turn.get",
        json!({"id": turn_id, "conversation_id": conversation_id}),
    )
    .unwrap()
}

fn catalog(database: &mut AuthorityDatabase) -> Vec<Value> {
    let mut cursor = Value::Null;
    let mut rows = Vec::new();
    loop {
        let value = execute(
            database,
            7,
            11,
            "migration.turn.scan",
            json!({"cursor": cursor}),
        )
        .unwrap();
        let page = value["rows"].as_array().unwrap();
        assert!(page.len() <= crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS);
        rows.extend(page.iter().cloned());
        cursor = value["cursor"].clone();
        if cursor.is_null() {
            return rows;
        }
    }
}

#[test]
fn turn_snapshot_preserves_identity_indexes_and_header_advancement() {
    let (vfs, mut database) = prepared();
    create_conversation(&mut database, "conversation");
    let document = snapshot("turn-alpha", "conversation", "main", 0, 9_000);
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    let mut branch = snapshot("turn-branch", "conversation", "analysis", 0, 9_100);
    branch["parent_turn_id"] = json!("turn-alpha");
    assert_eq!(
        import(&mut database, branch.clone()).unwrap()["inserted"],
        true
    );
    let next = snapshot("turn-beta", "conversation", "main", 1, 9_200);
    assert_eq!(
        import(&mut database, next.clone()).unwrap()["inserted"],
        true
    );
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/turns"), vfs).unwrap();
    assert_eq!(point(&mut database, "conversation", "turn-alpha"), document);
    assert_eq!(point(&mut database, "conversation", "turn-branch"), branch);
    assert_eq!(point(&mut database, "conversation", "turn-beta"), next);
    // Physical catalog order follows the owner-scoped turn document keys.
    assert_eq!(
        catalog(&mut database),
        vec![
            json!({"id": "turn-beta"}),
            json!({"id": "turn-alpha"}),
            json!({"id": "turn-branch"}),
        ]
    );
    // Offline import derives the main-lane count while preserving the header
    // revision and clock; replay cannot invent a new source event.
    let header = conversation_header(&database, "conversation");
    assert_eq!(header["metadata"]["msg_count"], 2);
    assert_eq!(header["metadata"]["rev"], 0);
    assert_eq!(header["metadata"]["updated_at"], 500);
    // The turn identity claims survive reopening and bind to the importer owner.
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(
        lookup_turn_claim(&database, &mut transaction, "turn-alpha").unwrap(),
        Some(11)
    );
    assert_eq!(
        lookup_turn_claim(&database, &mut transaction, "turn-branch").unwrap(),
        Some(11)
    );
    drop(transaction);
}

#[test]
fn turn_snapshot_rejects_divergence_cross_owner_and_missing_conversation() {
    let (_, mut database) = prepared();
    create_conversation(&mut database, "conversation");
    let document = snapshot("turn-alpha", "conversation", "main", 0, 9_000);
    import(&mut database, document.clone()).unwrap();
    for variant in 0..5 {
        let mut changed = document.clone();
        match variant {
            0 => changed["ordinal"] = json!(7),
            1 => changed["updated_at"] = json!(9_001),
            2 => changed["projection"] = json!({"content": "mutated"}),
            3 => changed["settlement"] = json!({"settled": false}),
            _ => changed["status"] = json!("failed"),
        }
        let error = import(&mut database, changed).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "variant {variant}");
    }
    // A live attempt can never be reconstructed by the offline path.
    let mut live = snapshot("turn-live", "conversation", "main", 1, 9_100);
    live["current_attempt_id"] = json!("attempt-1");
    assert_eq!(
        import(&mut database, live).unwrap_err().kind(),
        io::ErrorKind::InvalidInput
    );
    // Unknown conversations are rejected before any write.
    let missing = snapshot("turn-orphan", "missing", "main", 0, 9_200);
    assert_eq!(
        import(&mut database, missing).unwrap_err().kind(),
        io::ErrorKind::InvalidInput
    );
    // Owner and tenant scope bind every operation.
    assert!(execute(
        &mut database,
        7,
        12,
        "migration.turn.restore",
        document.clone()
    )
    .is_err());
    assert_eq!(
        execute(
            &mut database,
            7,
            12,
            "migration.turn.scan",
            json!({"cursor": Value::Null})
        )
        .unwrap()["rows"],
        json!([])
    );
    assert!(execute(
        &mut database,
        7,
        12,
        "migration.turn.get",
        json!({"id": "turn-alpha", "conversation_id": "conversation"})
    )
    .unwrap()
    .is_null());
    assert!(execute(
        &mut database,
        8,
        11,
        "migration.turn.get",
        json!({"id": "turn-alpha", "conversation_id": "conversation"})
    )
    .unwrap()
    .is_null());
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    // Ordinal/lane occupancy by a different identity fails closed.
    let occupant = snapshot("turn-other", "conversation", "main", 0, 9_300);
    assert_eq!(
        import(&mut database, occupant).unwrap_err().kind(),
        io::ErrorKind::WouldBlock
    );
}

#[test]
fn turn_catalog_pages_bind_scope_and_revalidate_targets() {
    let (_, mut database) = prepared();
    create_conversation(&mut database, "conversation");
    for index in 0..130_u64 {
        import(
            &mut database,
            snapshot(
                &format!("turn-{index:04}"),
                "conversation",
                "main",
                index,
                10_000 + index,
            ),
        )
        .unwrap();
    }
    assert_eq!(catalog(&mut database).len(), 130);
    let page = execute(
        &mut database,
        7,
        11,
        "migration.turn.scan",
        json!({"cursor": Value::Null}),
    )
    .unwrap();
    assert_eq!(
        page["rows"].as_array().unwrap().len(),
        crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS
    );
    // A catalog cursor belongs to its table and scope.
    assert!(execute(
        &mut database,
        7,
            11,
        "migration.turn.scan",
        json!({"cursor": format!("other:{}", page["cursor"].as_str().unwrap().split(':').nth(1).unwrap())})
    )
    .is_err());
    assert!(execute(
        &mut database,
        7,
        11,
        "migration.turn.scan",
        json!({"cursor": "turns:zz"})
    )
    .is_err());
    assert_eq!(
        execute(
            &mut database,
            7,
            12,
            "migration.turn.scan",
            json!({"cursor": Value::Null})
        )
        .unwrap()["rows"],
        json!([])
    );
    let header = conversation_header(&database, "conversation");
    assert_eq!(header["metadata"]["msg_count"], 130);
    assert_eq!(header["metadata"]["rev"], 0);
}

#[test]
fn turn_snapshot_and_indexes_recover_atomically_under_commit_faults() {
    let (baseline, mut database) = prepared();
    create_conversation(&mut database, "conversation");
    import(
        &mut database,
        snapshot("turn-one", "conversation", "main", 0, 9_000),
    )
    .unwrap();
    let trace = baseline.trace().unwrap();
    assert!(!trace.is_empty());
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
            let (vfs, mut database) = prepared();
            create_conversation(&mut database, "conversation");
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = import(
                &mut database,
                snapshot("turn-one", "conversation", "main", 0, 9_000),
            )
            .is_ok();
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut database = AuthorityDatabase::open_with_vfs(Path::new("/turns"), vfs).unwrap();
            let exists = !point(&mut database, "conversation", "turn-one").is_null();
            assert!(
                !acknowledged || exists,
                "lost acknowledged snapshot at {index}: {action:?}"
            );
            assert_eq!(
                catalog(&mut database).len(),
                usize::from(exists),
                "catalog {index} {action:?}"
            );
            let header = conversation_header(&database, "conversation");
            assert_eq!(
                header["metadata"]["msg_count"],
                u64::from(exists),
                "header {index} {action:?}"
            );
            import(
                &mut database,
                snapshot("turn-one", "conversation", "main", 0, 9_000),
            )
            .unwrap();
            assert_eq!(catalog(&mut database), vec![json!({"id": "turn-one"})]);
        }
    }
}
