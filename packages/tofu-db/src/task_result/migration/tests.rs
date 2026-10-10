use super::*;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use serde_json::json;
use std::{path::Path, sync::Arc};

fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/tasks")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let database =
        AuthorityDatabase::initialize_with_vfs(Path::new("/tasks"), vfs.clone()).unwrap();
    vfs.arm_fault(None).unwrap();
    (vfs, database)
}

fn snapshot(task_id: &str, body: Value, version: u64, updated_at_ms: u64) -> Value {
    let mut body = body.as_object().unwrap().clone();
    body.insert("task_id".into(), json!(task_id));
    body.insert("user_id".into(), json!(11));
    json!({
        "task_id": task_id,
        "owner_user_id": 11,
        "version": version,
        "updated_at_ms": updated_at_ms,
        "value_json": serde_json::to_string(&Value::Object(body)).unwrap(),
    })
}

fn sample(task_id: &str) -> Value {
    snapshot(
        task_id,
        json!({
            "conv_id": "conversation",
            "status": "completed",
            "created_at": 1_000,
            "completed_at": 2_000,
            "content": "result",
            "metadata": {"model": "alpha"},
            "error": null,
        }),
        7,
        123_456,
    )
}

fn import(database: &mut AuthorityDatabase, document: Value) -> io::Result<Value> {
    execute(database, 7, 11, "migration.task.result.restore", document)
}

fn point(database: &mut AuthorityDatabase, task_id: &str) -> Value {
    execute(
        database,
        7,
        11,
        "migration.task.result.get",
        json!({"task_id": task_id}),
    )
    .unwrap()
}

fn catalog(database: &mut AuthorityDatabase, table: &str) -> Vec<Value> {
    let mut cursor = Value::Null;
    let mut rows = Vec::new();
    loop {
        let value = execute(
            database,
            7,
            11,
            "migration.task.result.scan",
            json!({"table": table, "cursor": cursor}),
        )
        .unwrap();
        let page = value["rows"].as_array().unwrap();
        assert!(page.len() <= MIGRATION_SESSION_MAX_PAGE_ROWS);
        rows.extend(page.iter().cloned());
        cursor = value["cursor"].clone();
        if cursor.is_null() {
            return rows;
        }
    }
}

#[test]
fn task_result_snapshot_preserves_metadata_and_live_semantics_continue() {
    let (vfs, mut database) = prepared();
    let document = sample("task-alpha");
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    drop(database);
    vfs.crash().unwrap();
    let mut database = AuthorityDatabase::open_with_vfs(Path::new("/tasks"), vfs).unwrap();
    let observed = point(&mut database, "task-alpha");
    let mut expected = document.clone();
    expected["value_json"] =
        serde_json::from_str(document["value_json"].as_str().unwrap()).unwrap();
    assert_eq!(observed, expected);
    assert_eq!(
        catalog(&mut database, "headers"),
        vec![json!({"task_id": "task-alpha"})]
    );
    assert_eq!(
        catalog(&mut database, "summaries"),
        vec![json!({"task_id": "task-alpha"})]
    );
    assert!(catalog(&mut database, "live").is_empty());
    assert!(catalog(&mut database, "cost_index").is_empty());
    // Ordinary checkpoints continue from the imported version.
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let response = super::super::checkpoint(
        &database,
        &mut transaction,
        "task-alpha",
        serde_json::to_vec(&json!({
            "task_id": "task-alpha", "user_id": 11, "conv_id": "conversation",
            "status": "completed", "created_at": 1_000, "completed_at": 3_000,
            "content": "result-v2",
        }))
        .unwrap(),
        7,
        200_000,
        None,
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let response: Value = serde_json::from_slice(&response).unwrap();
    assert_eq!(response["version"], 8);
    assert_eq!(response["updated_at_ms"], 200_000);
    // A running task enters the live catalog and restart recovery settles it.
    import(
        &mut database,
        snapshot(
            "task-running",
            json!({"status": "running", "conv_id": "conversation"}),
            1,
            50,
        ),
    )
    .unwrap();
    assert_eq!(
        catalog(&mut database, "live"),
        vec![json!({"task_id": "task-running"})]
    );
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let recovered = super::super::recover_running(
        &database,
        &mut transaction,
        &super::super::RecoverRunningRequest {
            interrupted_reason: "restart".into(),
            maximum_rows: 10,
            scan_limit: 100,
            updated_at_ms: 60,
        },
    )
    .unwrap();
    database.commit(transaction).unwrap();
    let recovered: Value = serde_json::from_slice(&recovered).unwrap();
    assert_eq!(recovered["recovered"][0]["taskId"], "task-running");
    assert!(catalog(&mut database, "live").is_empty());
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let replay =
        super::super::replay_get(&database, &mut transaction, "task-running", 11, false, true)
            .unwrap()
            .unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&replay).unwrap()["status"],
        "interrupted"
    );
}

#[test]
fn task_result_snapshot_rejects_divergence_extras_and_cross_owner_state() {
    let (_, mut database) = prepared();
    let document = sample("task-alpha");
    import(&mut database, document.clone()).unwrap();
    for variant in 0..5 {
        let mut changed = document.clone();
        match variant {
            0 => changed["version"] = json!(8),
            1 => changed["updated_at_ms"] = json!(123_457),
            2 => {
                let mut body: Value =
                    serde_json::from_str(changed["value_json"].as_str().unwrap()).unwrap();
                body["content"] = json!("mutated");
                changed["value_json"] = json!(serde_json::to_string(&body).unwrap());
            }
            3 => {
                let mut body: Value =
                    serde_json::from_str(changed["value_json"].as_str().unwrap()).unwrap();
                body["abort_requested_at"] = json!(99);
                body["abort_source"] = json!("operator");
                changed["value_json"] = json!(serde_json::to_string(&body).unwrap());
            }
            _ => changed["owner_user_id"] = json!(12),
        }
        assert!(import(&mut database, changed).is_err(), "variant {variant}");
    }
    assert_eq!(
        import(&mut database, document.clone()).unwrap()["inserted"],
        false
    );
    // Tombstone merged by the abort semantic round-trips exactly.
    let tombstoned = snapshot(
        "task-abort",
        json!({
            "status": "running", "conv_id": "conversation",
            "abort_requested_at": 41, "abort_source": "user",
        }),
        3,
        40,
    );
    assert_eq!(
        import(&mut database, tombstoned.clone()).unwrap()["inserted"],
        true
    );
    assert_eq!(
        import(&mut database, tombstoned.clone()).unwrap()["inserted"],
        false
    );
    assert_eq!(
        catalog(&mut database, "live"),
        vec![json!({"task_id": "task-abort"})]
    );
    // Catalogs bind to the operator scope and reject foreign rows.
    assert_eq!(
        execute(
            &mut database,
            7,
            12,
            "migration.task.result.scan",
            json!({"table":"summaries","cursor":null})
        )
        .unwrap()["rows"],
        json!([])
    );
    assert!(execute(
        &mut database,
        7,
        12,
        "migration.task.result.restore",
        document
    )
    .is_err());
    // Cost-experiment indexes derive only from an explicit metadata outcome.
    let tagged = snapshot(
        "task-cost",
        json!({
            "status": "completed", "conv_id": "conversation", "completed_at": 9,
            "metadata": {"costExperiment": {"experimentId": "exp-1", "totalCostMicro": 5}},
        }),
        1,
        10,
    );
    import(&mut database, tagged).unwrap();
    assert_eq!(
        catalog(&mut database, "cost_index"),
        vec![json!({"task_id": "task-cost"})]
    );
}

#[test]
fn task_result_catalogs_page_bind_table_and_tenant() {
    let (_, mut database) = prepared();
    for index in 0..130 {
        import(
            &mut database,
            snapshot(
                &format!("task-{index:04}"),
                json!({"status": "completed", "conv_id": "conversation"}),
                1,
                1,
            ),
        )
        .unwrap();
    }
    assert_eq!(catalog(&mut database, "headers").len(), 130);
    assert_eq!(catalog(&mut database, "summaries").len(), 130);
    let page = execute(
        &mut database,
        7,
        11,
        "migration.task.result.scan",
        json!({"table": "headers", "cursor": null}),
    )
    .unwrap();
    assert_eq!(
        page["rows"].as_array().unwrap().len(),
        MIGRATION_SESSION_MAX_PAGE_ROWS
    );
    assert!(execute(
        &mut database,
        7,
        11,
        "migration.task.result.scan",
        json!({"table": "summaries", "cursor": page["cursor"]})
    )
    .is_err());
    assert!(execute(
        &mut database,
        8,
        11,
        "migration.task.result.scan",
        json!({"table": "headers", "cursor": null})
    )
    .unwrap()["rows"]
        .as_array()
        .unwrap()
        .is_empty());
    assert!(execute(
        &mut database,
        7,
        11,
        "migration.task.result.scan",
        json!({"table": "unknown", "cursor": null})
    )
    .is_err());
}

#[test]
fn task_result_snapshot_and_indexes_recover_atomically_under_commit_faults() {
    let (baseline, mut database) = prepared();
    import(&mut database, sample("task-one")).unwrap();
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
            let acknowledged = import(&mut database, sample("task-one")).is_ok();
            drop(database);
            vfs.crash().unwrap();
            vfs.arm_fault(None).unwrap();
            let mut database = AuthorityDatabase::open_with_vfs(Path::new("/tasks"), vfs).unwrap();
            let exists = !point(&mut database, "task-one").is_null();
            assert!(
                !acknowledged || exists,
                "lost acknowledged snapshot at {index}: {action:?}"
            );
            for table in ["headers", "summaries"] {
                assert_eq!(
                    catalog(&mut database, table).len(),
                    usize::from(exists),
                    "{table} {index} {action:?}"
                );
            }
            import(&mut database, sample("task-one")).unwrap();
            assert_eq!(
                catalog(&mut database, "summaries"),
                vec![json!({"task_id": "task-one"})]
            );
        }
    }
}
