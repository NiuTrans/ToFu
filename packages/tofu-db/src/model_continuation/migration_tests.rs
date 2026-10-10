use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use sha2::{Digest, Sha256};
use std::{path::Path, sync::Arc};

fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/continuations")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database =
        AuthorityDatabase::initialize_with_vfs(Path::new("/continuations"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, database)
}

fn digest(text: &str) -> String {
    let digest = Sha256::digest(text.as_bytes());
    let mut hex = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut hex, "{byte:02x}").expect("writing to String cannot fail");
    }
    hex
}

fn record_key(owner: u64, conversation_id: &str, lane_id: &str) -> String {
    format!("{}:{}:{}", owner, digest(conversation_id), digest(lane_id))
}

fn projection(turn_id: &str) -> Value {
    json!({
        "messages": [
            {"role": "user", "content": "question".repeat(2048)},
            {"role": "assistant", "content": format!("answer-{turn_id}")},
        ],
        "turns": [[turn_id, digest(turn_id)]],
    })
}

fn canonical(value: &Value) -> String {
    // The live authority stores compact JSON preserving insertion order.
    serde_json::to_string(value).expect("projection encodes")
}

fn snapshot(owner: u64, conversation_id: &str, lane_id: &str, turn_id: &str) -> Value {
    json!({
        "record_key": record_key(owner, conversation_id, lane_id),
        "value_json": canonical(&projection(turn_id)),
        "version": 7,
        "updated_at_ms": 1_700_000_000_000_i64,
    })
}

fn import(database: &mut AuthorityDatabase, document: Value) -> io::Result<Value> {
    execute(database, 7, 11, "migration.continuation.restore", document)
}

fn point(
    database: &mut AuthorityDatabase,
    owner: u64,
    conversation_id: &str,
    lane_id: &str,
) -> Value {
    execute(
        database,
        7,
        owner,
        "migration.continuation.get",
        json!({"record_key": record_key(owner, conversation_id, lane_id)}),
    )
    .unwrap()
}

fn catalog(database: &mut AuthorityDatabase, owner: u64) -> Vec<Value> {
    let mut cursor = Value::Null;
    let mut rows = Vec::new();
    loop {
        let value = execute(
            database,
            7,
            owner,
            "migration.continuation.scan",
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
fn continuation_snapshot_preserves_projection_frontier_and_timestamp() {
    let (vfs, mut database) = prepared();
    let document = snapshot(11, "conversation", "main", "turn-a");
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    let branch = snapshot(11, "conversation", "analysis", "turn-b");
    assert_eq!(
        import(&mut database, branch.clone()).unwrap()["inserted"],
        true
    );
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/continuations"), vfs).unwrap();
    assert_eq!(point(&mut database, 11, "conversation", "main"), document);
    assert_eq!(point(&mut database, 11, "conversation", "analysis"), branch);
    // The live reader continues from the restored frontier and projection.
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let key = super::super::continuation_key(&transaction, "conversation", "main").unwrap();
    let (frontier, stored) = read_record(&database, &mut transaction, &key)
        .unwrap()
        .expect("restored continuation record");
    assert_eq!(frontier, 7);
    assert_eq!(
        encode_projection(&Value::Object(stored)).unwrap(),
        document["value_json"].as_str().unwrap().as_bytes()
    );
}

#[test]
fn continuation_snapshot_rejects_divergence_cross_owner_and_malformed() {
    let (_, mut database) = prepared();
    let document = snapshot(11, "conversation", "main", "turn-a");
    import(&mut database, document.clone()).unwrap();
    for variant in 0..4 {
        let mut changed = document.clone();
        match variant {
            0 => changed["version"] = json!(8),
            1 => changed["updated_at_ms"] = json!(1_700_000_000_001_i64),
            2 => {
                let mut body = projection("turn-a");
                body["messages"][1]["content"] = json!("mutated");
                changed["value_json"] = json!(canonical(&body));
            }
            _ => changed["value_json"] = json!(canonical(&projection("turn-z"))),
        }
        let error = import(&mut database, changed).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::WouldBlock, "variant {variant}");
    }
    // A foreign owner id inside the record key is outside the operator scope.
    let foreign = snapshot(12, "conversation", "main", "turn-c");
    assert_eq!(
        import(&mut database, foreign).unwrap_err().kind(),
        io::ErrorKind::PermissionDenied
    );
    // Owner and tenant scope bind every operation.
    assert!(execute(
        &mut database,
        7,
        12,
        "migration.continuation.restore",
        document.clone()
    )
    .is_err());
    assert!(point(&mut database, 12, "conversation", "main").is_null());
    assert!(execute(
        &mut database,
        8,
        11,
        "migration.continuation.get",
        json!({"record_key": record_key(11, "conversation", "main")})
    )
    .unwrap()
    .is_null());
    assert_eq!(catalog(&mut database, 12), Vec::<Value>::new());
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    // Malformed projections and digests never reach storage.
    for variant in 0..4 {
        let mut bad = snapshot(11, "conversation", "main", "turn-d");
        match variant {
            0 => bad["value_json"] = json!("{}"),
            1 => {
                let mut body = projection("turn-d");
                body["messages"][1]["role"] = json!("user");
                bad["value_json"] = json!(canonical(&body));
            }
            2 => bad["record_key"] = json!("11:nothex:nothex"),
            _ => bad["version"] = json!(0),
        }
        assert!(import(&mut database, bad).is_err(), "variant {variant}");
    }
}

#[test]
fn continuation_catalog_pages_bind_scope_and_revalidate_targets() {
    let (_, mut database) = prepared();
    for index in 0..130_u64 {
        import(
            &mut database,
            snapshot(
                11,
                &format!("conversation-{index:04}"),
                "main",
                &format!("turn-{index:04}"),
            ),
        )
        .unwrap();
    }
    assert_eq!(catalog(&mut database, 11).len(), 130);
    let page = execute(
        &mut database,
        7,
        11,
        "migration.continuation.scan",
        json!({"cursor": Value::Null}),
    )
    .unwrap();
    assert_eq!(
        page["rows"].as_array().unwrap().len(),
        crate::generated_tofudb_ir::MIGRATION_SESSION_MAX_PAGE_ROWS.min(4)
    );
    // A catalog cursor belongs to its table and scope.
    let suffix = page["cursor"].as_str().unwrap().split(':').nth(1).unwrap();
    assert!(execute(
        &mut database,
        7,
        11,
        "migration.continuation.scan",
        json!({"cursor": format!("other:{suffix}")})
    )
    .is_err());
    assert!(execute(
        &mut database,
        7,
        11,
        "migration.continuation.scan",
        json!({"cursor": "continuations:zz"})
    )
    .is_err());
    assert_eq!(catalog(&mut database, 12), Vec::<Value>::new());
}

#[test]
fn continuation_snapshot_recovers_atomically_under_commit_faults() {
    let (baseline, mut database) = prepared();
    import(
        &mut database,
        snapshot(11, "conversation", "main", "turn-a"),
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
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action,
            }))
            .unwrap();
            let acknowledged = import(
                &mut database,
                snapshot(11, "conversation", "main", "turn-a"),
            )
            .is_ok();
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut database =
                AuthorityDatabase::open_with_vfs(Path::new("/continuations"), vfs).unwrap();
            let exists = !point(&mut database, 11, "conversation", "main").is_null();
            assert!(
                !acknowledged || exists,
                "lost acknowledged snapshot at {index}: {action:?}"
            );
            assert_eq!(
                catalog(&mut database, 11).len(),
                usize::from(exists),
                "catalog {index} {action:?}"
            );
            import(
                &mut database,
                snapshot(11, "conversation", "main", "turn-a"),
            )
            .unwrap();
            assert_eq!(
                catalog(&mut database, 11),
                vec![json!({"record_key": record_key(11, "conversation", "main")})]
            );
        }
    }
}

#[test]
fn continuation_blob_survives_gc_and_reopen_with_exact_import_clocks() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let mut document = snapshot(11, "large", "main", "turn-large");
    let mut body = projection("turn-large");
    body["messages"][0]["content"] = json!("x".repeat(256 * 1024));
    document["value_json"] = json!(canonical(&body));
    document["updated_at_ms"] = json!(123456789);
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        true
    );
    {
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let key = super::super::continuation_key(&tx, "large", "main").unwrap();
        let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
        assert!(crate::versioned_document::stored_blob_reference(&stored)
            .unwrap()
            .is_some());
    }
    database
        .collect_garbage(crate::authority_gc::AuthorityGarbageCollectionBudget::conservative())
        .unwrap();
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(point(&mut database, 11, "large", "main"), document);
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    assert_eq!(catalog(&mut database, 11).len(), 1);
    assert!(point(&mut database, 12, "large", "main").is_null());
}

#[test]
fn continuation_legacy_reads_and_equal_replay_do_not_rewrite_but_advance_upgrades() {
    let (_, mut database) = prepared();
    let mut body = projection("legacy-turn").as_object().unwrap().clone();
    // This case starts from the historical inline format, then advances to a blob.
    body.get_mut("messages").unwrap()[0]["content"] = json!("legacy question");
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let key = super::super::continuation_key(&tx, "legacy", "main").unwrap();
    let raw = serde_json::to_vec(&json_record(7, &body, 100)).unwrap();
    database
        .entity_put(&mut tx, key.clone(), raw.clone())
        .unwrap();
    database.commit(tx).unwrap();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(
        read_record(&database, &mut tx, &key).unwrap(),
        Some((7, body.clone()))
    );
    assert!(super::super::put_frontier(&database, &mut tx, key.clone(), &super::super::continuation_raw_key("legacy", "main"), 7, &body, 200).unwrap());
    assert_eq!(database.entity_get(&mut tx, &key).unwrap().unwrap(), raw);
    drop(tx);
    let mut large = body;
    large.get_mut("messages").unwrap()[0]["content"] = json!("x".repeat(16 * 1024));
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(super::super::put_frontier(&database, &mut tx, key.clone(), &super::super::continuation_raw_key("legacy", "main"), 8, &large, 300).unwrap());
    // Dropping the transaction must leave the legacy record intact.
    drop(tx);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(database.entity_get(&mut tx, &key).unwrap().unwrap(), raw);
    assert!(super::super::put_frontier(&database, &mut tx, key.clone(), &super::super::continuation_raw_key("legacy", "main"), 8, &large, 300).unwrap());
    database.commit(tx).unwrap();
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(
        read_record(&database, &mut tx, &key).unwrap(),
        Some((8, large))
    );
    let stored = database.entity_get(&mut tx, &key).unwrap().unwrap();
    assert!(crate::versioned_document::stored_blob_reference(&stored)
        .unwrap()
        .is_some());
}
