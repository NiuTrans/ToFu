// Recovery byte evidence stays atomic with live projection edits. Legacy
// repair requires a retained event and the exact appended steer suffix.
fn recovery_bytes_key(attempt: &Value) -> EntityKey {
    let mut key = attempt["createdAt"].as_u64().unwrap().to_be_bytes().to_vec();
    key.extend_from_slice(attempt["attemptId"].as_str().unwrap().as_bytes());
    EntityKey::new(7, 11, crate::generated_tofudb_ir::ATTEMPT_RECOVERY_INDEX_NAMESPACE, &key).unwrap()
}

fn legacy_steer_bytes_fixture(database: &mut AuthorityDatabase, conversation: &str,
    retained_injections: bool) -> (Request, EntityKey, Vec<u8>) {
    let pair = accepted_executor_pair(database, 11, conversation, 1_000);
    let attempt = &pair["attempt"];
    let attempt_id = attempt["attemptId"].as_str().unwrap();
    execute(database, &request("turn.attempt.bind", 11,
        json!({"attempt_id":attempt_id,"task_id":"legacy-steer-task","user_id":11}),
        Some(&format!("{conversation}-bind"))), 1_010).unwrap();
    let steer = |index| request("turn.steer.commit", 11,
        json!({"conversation_id":conversation,"attempt_id":attempt_id,
            "command_id":format!("{conversation}-steer-{index}"),"text":"继续 🧪\n保留全部内容",
            "author":{"actor":"human"},"user_id":11}),
        Some(&format!("{conversation}-steer-{index}")));
    if retained_injections { execute(database, &steer(0), 1_011).unwrap(); }
    let read = request("turn.get", 11, json!({"conversation_id":conversation,
        "turn_id":attempt["turnId"],"user_id":11}), None);
    let mut projection = execute(database, &read, 1_012).unwrap()["projection"].clone();
    projection["content"] = json!("unchanged complete content");
    execute(database, &request("turn.event.record", 11,
        json!({"attempt_id":attempt_id,"task_id":"legacy-steer-task","user_id":11,
            "status":"running","event_type":"projection_updated","event_payload":{},
            "projection":projection}), Some(&format!("{conversation}-event"))), 1_020).unwrap();
    let key = recovery_bytes_key(attempt);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let old = database.entity_get(&mut tx, &key).unwrap().unwrap();
    drop(tx);
    for index in 1..=2 { execute(database, &steer(index), 1_030 + index).unwrap(); }
    // Exactly the pre-fix writer: committed projection, unchanged index.
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    database.entity_put(&mut tx, key.clone(), old.clone()).unwrap();
    database.commit(tx).unwrap();
    (read, key, old)
}

#[test]
fn legacy_steer_recovery_bytes_require_exact_retained_event_evidence() {
    for retained in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let (read, _, _) = legacy_steer_bytes_fixture(&mut database, "legacy-steer", retained);
        let before = execute(&mut database, &read, 1_100).unwrap();
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(recover_owner(&mut database, 11, 2_000)["recovered"], 1);
        let after = execute(&mut database, &read, 2_001).unwrap();
        assert_eq!(after["projection"], before["projection"]);
        assert_eq!(after["status"], "interrupted");
        assert_eq!(recover_owner(&mut database, 11, 2_002)["recovered"], 0);
    }
}

#[test]
fn legacy_steer_recovery_bytes_do_not_repair_arbitrary_index_corruption() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let (read, key, old) = legacy_steer_bytes_fixture(&mut database, "bad-steer", true);
    let before = execute(&mut database, &read, 1_100).unwrap();
    let mut bad: Value = serde_json::from_slice(&old).unwrap();
    bad["projectionBytes"] = json!(bad["projectionBytes"].as_u64().unwrap() + 1);
    let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    database.entity_put(&mut tx, key, serde_json::to_vec(&bad).unwrap()).unwrap();
    database.commit(tx).unwrap();
    let result = execute(&mut database, &request("turn.recover", 11,
        json!({"user_id":11}), Some("bad-steer-recovery")), 2_000);
    assert!(matches!(result, Err(SemanticExecutionError::Integrity(_))));
    assert_eq!(execute(&mut database, &read, 2_001).unwrap(), before);
}

#[test]
fn legacy_steer_recovery_bytes_reject_missing_foreign_and_changed_event_evidence() {
    for corruption in ["missing", "foreign", "revision", "bytes"] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let (read, _, locator) = legacy_steer_bytes_fixture(&mut database, "evidence-steer", true);
        let before = execute(&mut database, &read, 1_100).unwrap();
        let attempt: Value = serde_json::from_slice(&locator).unwrap();
        let attempt_id = attempt["attemptId"].as_str().unwrap();
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let head_key = crate::turn::attempt_event_head_key(&tx, attempt_id).unwrap();
        let head = database.entity_get(&mut tx, &head_key).unwrap().unwrap();
        let sequence = u64::from_le_bytes(head.try_into().unwrap());
        let key = crate::turn::attempt_event_key(&tx, attempt_id, sequence).unwrap();
        if corruption == "missing" {
            database.entity_delete(&mut tx, key).unwrap();
        } else {
            let logical = format!("{attempt_id}\u{1f}{sequence:020}");
            let (raw, version) = crate::versioned_document::get_value_with_version(
                &database, &mut tx, &key, "attempt_events", &logical).unwrap().unwrap();
            let mut event: Value = serde_json::from_slice(&raw).unwrap();
            match corruption {
                "foreign" => event["conversationId"] = json!("another-owner"),
                "revision" => event["projectionRevision"] = json!(0),
                "bytes" => event["payload"]["projectionBytes"] = json!(1),
                _ => unreachable!(),
            }
            crate::versioned_document::put(&database, &mut tx,
                crate::versioned_document::PutRequest { key, namespace:"attempt_events".into(),
                    logical_key:logical, value_json:serde_json::to_vec(&event).unwrap(),
                    expected_version:Some(version), updated_at_ms:1_200 }).unwrap();
        }
        database.commit(tx).unwrap();
        assert!(matches!(execute(&mut database, &request("turn.recover", 11,
            json!({"user_id":11}), Some("bad-event-recovery")), 2_000),
            Err(SemanticExecutionError::Integrity(_))), "{corruption}");
        assert_eq!(execute(&mut database, &read, 2_001).unwrap(), before);
    }
}

#[test]
fn legacy_steer_recovery_bytes_use_actual_size_for_page_budget() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let (read_a, _, _) = legacy_steer_bytes_fixture(&mut database, "page-a", false);
    let (read_b, _, _) = legacy_steer_bytes_fixture(&mut database, "page-b", false);
    let before_a = execute(&mut database, &read_a, 1_100).unwrap();
    let before_b = execute(&mut database, &read_b, 1_100).unwrap();
    let total = serde_json::to_vec(&before_a["projection"]).unwrap().len()
        + serde_json::to_vec(&before_b["projection"]).unwrap().len();
    let recover = |id| request("turn.recover", 11,
        json!({"user_id":11,"max_bytes":total - 1}), Some(id));
    let first = execute(&mut database, &recover("page-first"), 2_000).unwrap();
    assert_eq!(first["recovered"], 1);
    assert_eq!(first["remaining"], 1);
    assert_eq!(execute(&mut database, &recover("page-next"), 2_001).unwrap()["recovered"], 1);
    assert_eq!(execute(&mut database, &read_a, 2_002).unwrap()["projection"], before_a["projection"]);
    assert_eq!(execute(&mut database, &read_b, 2_002).unwrap()["projection"], before_b["projection"]);
}

#[test]
fn legacy_steer_recovery_bytes_settle_atomically_at_each_commit_fault() {
    let initial = prepared_vfs();
    let mut database = AuthorityDatabase::initialize_with_vfs(
        std::path::Path::new("/data"), initial.clone()).unwrap();
    let (read, key, old) = legacy_steer_bytes_fixture(&mut database, "fault-legacy", true);
    let image = initial.fork_durable().unwrap();
    drop(database);
    let seed = || {
        let vfs = std::sync::Arc::new(image.fork_durable().unwrap());
        let mut database = AuthorityDatabase::open_with_vfs(
            std::path::Path::new("/data"), vfs.clone()).unwrap();
        database.configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024).unwrap();
        (vfs, database)
    };
    let command = request("turn.recover", 11, json!({"user_id":11}), Some("legacy-recover"));
    let (vfs, mut database) = seed();
    let before = execute(&mut database, &read, 1_100).unwrap();
    vfs.arm_fault(None).unwrap();
    execute(&mut database, &command, 2_000).unwrap();
    let trace = vfs.trace().unwrap();
    let after = execute(&mut database, &read, 2_001).unwrap();
    drop(database);
    for (index, operation) in trace.iter().enumerate() {
        if !matches!(operation, Operation::Write | Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory) {
            continue;
        }
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        else { actions.push(FaultAction::DropSync); }
        for action in actions {
            let (vfs, mut database) = seed();
            execute(&mut database, &read, 1_100).unwrap();
            let occurrence = trace[..=index].iter().filter(|item| *item == operation).count();
            vfs.arm_operation_fault(*operation, occurrence, action).unwrap();
            let acknowledged = execute(&mut database, &command, 2_000).is_ok();
            assert!(vfs.operation_fault_fired().unwrap());
            let image = std::sync::Arc::new(vfs.fork_durable().unwrap());
            drop(database);
            let mut reopened = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), image).unwrap();
            let actual = execute(&mut reopened, &read, 2_001).unwrap();
            assert!(actual == before || actual == after, "partial recovery at {index}");
            if acknowledged { assert_eq!(actual, after); }
            let mut tx = reopened.begin_with_identity_claim_scopes(7, 11).unwrap();
            assert_eq!(reopened.entity_get(&mut tx, &key).unwrap(),
                if actual == before { Some(old.clone()) } else { None });
            drop(tx);
            reopened.configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024).unwrap();
            assert_eq!(reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
                u64::from(actual == after));
            execute(&mut reopened, &command, 2_000).unwrap();
            assert_eq!(execute(&mut reopened, &read, 2_001).unwrap(), after);
            assert_eq!(reopened.logical_outbox_status(7, 11).unwrap().last_sequence, 1);
        }
    }
}
