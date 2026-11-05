// The accepted-attempt dispatchable index is a tenant-global catalogue of
// `pending`, unbound, unqueued conversation-executor attempts. The serving
// loop's backstop lists it every few seconds and fails closed on any entry
// whose target left that state, so every transition out of the state must
// retire the entry in the same transaction, and restart recovery must retire
// entries an earlier engine left behind without masking corrupt rows.

fn dispatchable_attempts(
    database: &mut AuthorityDatabase,
    now_ms: u64,
) -> Result<Value, SemanticExecutionError> {
    execute(
        database,
        &request(
            "turn.attempt.dispatchable.list",
            99,
            json!({"created_before_ms": now_ms, "limit": 32}),
            None,
        ),
        now_ms,
    )
}

fn accepted_executor_pair(
    database: &mut AuthorityDatabase,
    owner_user_id: u64,
    conversation_id: &str,
    now_ms: u64,
) -> Value {
    execute(
        database,
        &request(
            "turn.create_pair",
            owner_user_id,
            json!({
                "conversation_id": conversation_id,
                "user_id": owner_user_id,
                "command_id": format!("{conversation_id}-command"),
                "input_projection": {"content": "hello"},
                "dispatch_mode": "conversation_executor",
                "config": {"model": "dispatch-model"},
                "conversation_defaults": {"allowCreate": true, "title": "Dispatch"},
                "now": now_ms
            }),
            Some(&format!("{conversation_id}-receipt")),
        ),
        now_ms,
    )
    .unwrap()
}

fn listed_attempt_ids(listed: &Value) -> Vec<String> {
    listed
        .as_array()
        .unwrap()
        .iter()
        .map(|item| item["attempt"]["attemptId"].as_str().unwrap().to_owned())
        .collect()
}

/// Re-create the entry exactly as the pre-fix writers left it behind.
fn plant_dispatchable_entry(
    database: &mut AuthorityDatabase,
    owner_user_id: u64,
    conversation_id: &str,
    turn_id: &str,
    attempt_id: &str,
    created_at_ms: u64,
) {
    let mut key = created_at_ms.to_be_bytes().to_vec();
    key.extend_from_slice(attempt_id.as_bytes());
    let mut transaction = database
        .begin_with_identity_claim_scopes(7, owner_user_id)
        .unwrap();
    database
        .entity_put(
            &mut transaction,
            EntityKey::new(
                7,
                crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
                crate::generated_tofudb_ir::ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
                &key,
            )
            .unwrap(),
            serde_json::to_vec(&json!({
                "userId": owner_user_id,
                "conversationId": conversation_id,
                "turnId": turn_id,
                "attemptId": attempt_id
            }))
            .unwrap(),
        )
        .unwrap();
    database.commit(transaction).unwrap();
}

fn dispatchable_index_rows(database: &mut AuthorityDatabase) -> usize {
    let (start, end) = EntityKey::prefix_range(
        7,
        crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
        crate::generated_tofudb_ir::ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
        b"",
    )
    .unwrap();
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    database
        .entity_scan(&mut transaction, &start, &end, 64)
        .unwrap()
        .len()
}

fn recover_owner(database: &mut AuthorityDatabase, owner_user_id: u64, now_ms: u64) -> Value {
    execute(
        database,
        &request(
            "turn.recover",
            owner_user_id,
            json!({"user_id": owner_user_id, "created_before_ms": now_ms}),
            Some(&format!("turn-recover:{owner_user_id}:{now_ms}")),
        ),
        now_ms,
    )
    .unwrap()
}

#[test]
fn restart_recovery_retires_the_dispatchable_entry_of_the_attempt_it_settles() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let accepted = accepted_executor_pair(&mut database, 11, "recover-dispatch", 1_000);
    let attempt_id = accepted["attempt"]["attemptId"].as_str().unwrap();
    assert_eq!(
        listed_attempt_ids(&dispatchable_attempts(&mut database, 1_500).unwrap()),
        [attempt_id]
    );

    assert_eq!(recover_owner(&mut database, 11, 2_000)["recovered"], 1);

    assert_eq!(
        execute(
            &mut database,
            &request(
                "turn.attempt.get",
                11,
                json!({"attempt_id": attempt_id, "user_id": 11}),
                None,
            ),
            2_100,
        )
        .unwrap()["status"],
        "interrupted"
    );
    assert_eq!(
        dispatchable_attempts(&mut database, 3_000).unwrap(),
        json!([])
    );
}

#[test]
fn terminal_settlement_before_binding_retires_the_dispatchable_entry() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let accepted = accepted_executor_pair(&mut database, 11, "abort-dispatch", 1_000);
    let attempt_id = accepted["attempt"]["attemptId"].as_str().unwrap();

    // An owner abort before any executor claimed the attempt settles it with
    // an empty task identity (`lib.turn_lifecycle.abort_attempt`).
    let settled = execute(
        &mut database,
        &request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id,
                "user_id": 11,
                "terminal": true,
                "status": "interrupted",
                "settlement": {"outcome": "interrupted", "cause": "user", "resumeOptions": []},
                "now": 1_200
            }),
            Some("abort-dispatch-settle"),
        ),
        1_200,
    )
    .unwrap();
    assert_eq!(settled["applied"], true);

    assert_eq!(
        dispatchable_attempts(&mut database, 3_000).unwrap(),
        json!([])
    );
}

#[test]
fn conversation_delete_retires_the_dispatchable_entry_of_its_unclaimed_attempt() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    accepted_executor_pair(&mut database, 11, "deleted-dispatch", 1_000);
    let kept = accepted_executor_pair(&mut database, 11, "kept-dispatch", 1_100);

    execute(
        &mut database,
        &request(
            "conversation.delete",
            11,
            json!({"conv_id": "deleted-dispatch", "user_id": 11}),
            Some("deleted-dispatch-delete"),
        ),
        1_500,
    )
    .unwrap();

    assert_eq!(
        listed_attempt_ids(&dispatchable_attempts(&mut database, 3_000).unwrap()),
        [kept["attempt"]["attemptId"].as_str().unwrap()]
    );
}

#[test]
fn queue_activation_publishes_and_claim_retires_the_dispatchable_entry() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let queued = execute(
        &mut database,
        &request(
            "turn.create_pair",
            11,
            json!({
                "conversation_id": "queued-dispatch",
                "user_id": 11,
                "command_id": "queued-dispatch-command",
                "input_projection": {"content": "queued"},
                "dispatch_mode": "conversation_executor",
                "queue_binding": {"queueId": "queued-dispatch-queue", "kind": "real", "priority": 1},
                "conversation_defaults": {"allowCreate": true},
                "now": 1_000
            }),
            Some("queued-dispatch-receipt"),
        ),
        1_000,
    )
    .unwrap();
    assert_eq!(queued["queued"], true);
    assert_eq!(
        dispatchable_attempts(&mut database, 1_500).unwrap(),
        json!([])
    );

    let activated = execute(
        &mut database,
        &request(
            "turn.queue.activate",
            11,
            json!({
                "conversation_id": "queued-dispatch",
                "queue_id": "queued-dispatch-queue",
                "user_id": 11
            }),
            Some("queued-dispatch-activate"),
        ),
        2_000,
    )
    .unwrap();
    let attempt_id = activated["attempt"]["attemptId"].as_str().unwrap();
    assert_eq!(
        listed_attempt_ids(&dispatchable_attempts(&mut database, 2_500).unwrap()),
        [attempt_id]
    );

    let claimed = execute(
        &mut database,
        &request(
            "turn.attempt.claim",
            11,
            json!({"attempt_id": attempt_id, "user_id": 11}),
            Some("queued-dispatch-claim"),
        ),
        3_000,
    )
    .unwrap();
    assert_eq!(claimed, true);
    assert_eq!(
        dispatchable_attempts(&mut database, 3_500).unwrap(),
        json!([])
    );
}

#[test]
fn restart_recovery_repairs_dispatchable_entries_stranded_by_earlier_engines() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let settled = accepted_executor_pair(&mut database, 11, "stranded-settled", 1_000);
    let deleted = accepted_executor_pair(&mut database, 11, "stranded-deleted", 1_050);
    let foreign = accepted_executor_pair(&mut database, 12, "stranded-foreign", 1_100);
    assert_eq!(recover_owner(&mut database, 11, 1_200)["recovered"], 2);
    assert_eq!(recover_owner(&mut database, 12, 1_200)["recovered"], 1);
    execute(
        &mut database,
        &request(
            "conversation.delete",
            11,
            json!({"conv_id": "stranded-deleted", "user_id": 11}),
            Some("stranded-deleted-delete"),
        ),
        1_300,
    )
    .unwrap();
    for (owner_user_id, accepted) in [(11, &settled), (11, &deleted), (12, &foreign)] {
        plant_dispatchable_entry(
            &mut database,
            owner_user_id,
            accepted["attempt"]["conversationId"].as_str().unwrap(),
            accepted["turn"]["turnId"].as_str().unwrap(),
            accepted["attempt"]["attemptId"].as_str().unwrap(),
            accepted["attempt"]["createdAt"].as_u64().unwrap(),
        );
    }
    let live = accepted_executor_pair(&mut database, 11, "still-dispatchable", 2_000);
    let live_attempt_id = live["attempt"]["attemptId"].as_str().unwrap();
    assert_eq!(
        dispatchable_attempts(&mut database, 2_500),
        Err(integrity_failure(
            "attempt dispatchable index target differs"
        ))
    );

    // Recovery repairs only its owner's entries whose target is settled or
    // whose conversation is gone; the live accepted attempt created after
    // the cutoff keeps its entry.
    assert_eq!(recover_owner(&mut database, 11, 1_500)["recovered"], 0);
    assert_eq!(
        dispatchable_attempts(&mut database, 2_500),
        Err(integrity_failure(
            "attempt dispatchable index target differs"
        ))
    );
    assert_eq!(recover_owner(&mut database, 12, 1_500)["recovered"], 0);
    assert_eq!(
        listed_attempt_ids(&dispatchable_attempts(&mut database, 2_500).unwrap()),
        [live_attempt_id]
    );
}

#[test]
fn restart_recovery_keeps_corrupt_dispatchable_entries_fail_closed() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let accepted = accepted_executor_pair(&mut database, 11, "corrupt-dispatch", 1_000);
    let conversation_id = accepted["attempt"]["conversationId"].as_str().unwrap();
    let turn_id = accepted["turn"]["turnId"].as_str().unwrap();
    let attempt_id = accepted["attempt"]["attemptId"].as_str().unwrap();
    assert_eq!(recover_owner(&mut database, 11, 1_100)["recovered"], 1);

    // A creation clock that disagrees with the settled target is not
    // explained by any writer; neither is a target that never existed.
    plant_dispatchable_entry(&mut database, 11, conversation_id, turn_id, attempt_id, 900);
    plant_dispatchable_entry(
        &mut database,
        11,
        conversation_id,
        turn_id,
        "missing-attempt",
        950,
    );
    assert_eq!(dispatchable_index_rows(&mut database), 2);
    assert_eq!(recover_owner(&mut database, 11, 1_200)["recovered"], 0);
    assert_eq!(dispatchable_index_rows(&mut database), 2);
    assert_eq!(
        dispatchable_attempts(&mut database, 2_000),
        Err(integrity_failure(
            "attempt dispatchable index target differs"
        ))
    );
}

#[test]
fn restart_recovery_keeps_unproven_missing_dispatchable_targets() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let accepted = accepted_executor_pair(&mut database, 11, "deleted-proof", 1_000);
    execute(
        &mut database,
        &request(
            "conversation.delete",
            11,
            json!({"conv_id": "deleted-proof", "user_id": 11}),
            Some("deleted-proof-delete"),
        ),
        1_200,
    )
    .unwrap();
    // A missing conversation is not proof that a fabricated attempt existed.
    plant_dispatchable_entry(&mut database, 11, "never-created", "turn", "missing", 900);
    // Even a real trash capsule must contain this exact indexed attempt.
    plant_dispatchable_entry(
        &mut database,
        11,
        "deleted-proof",
        accepted["turn"]["turnId"].as_str().unwrap(),
        "never-created-attempt",
        1_000,
    );
    recover_owner(&mut database, 11, 2_000);
    assert_eq!(dispatchable_index_rows(&mut database), 2);
    assert!(dispatchable_attempts(&mut database, 3_000).is_err());
}

#[test]
fn restart_recovery_repairs_deleted_recovery_and_dispatchable_indexes() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let accepted = accepted_executor_pair(&mut database, 11, "deleted-recovery", 1_000);
    let attempt_id = accepted["attempt"]["attemptId"].as_str().unwrap();
    let mut encoded = 1_000_u64.to_be_bytes().to_vec();
    encoded.extend_from_slice(attempt_id.as_bytes());
    let key = EntityKey::new(
        7,
        11,
        crate::generated_tofudb_ir::ATTEMPT_RECOVERY_INDEX_NAMESPACE,
        &encoded,
    )
    .unwrap();
    let old_locator = {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        database
            .entity_get(&mut transaction, &key)
            .unwrap()
            .unwrap()
    };
    execute(
        &mut database,
        &request(
            "conversation.delete",
            11,
            json!({"conv_id": "deleted-recovery", "user_id": 11}),
            Some("deleted-recovery-delete"),
        ),
        1_200,
    )
    .unwrap();
    assert_eq!(recover_owner(&mut database, 11, 1_500)["recovered"], 0);
    // Recreate the old writer's exact stranded row, rather than fabricating
    // a locator that would weaken the retained-trash identity proof.
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    database
        .entity_put(&mut transaction, key.clone(), old_locator)
        .unwrap();
    database.commit(transaction).unwrap();
    assert_eq!(recover_owner(&mut database, 11, 2_000)["recovered"], 0);
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(database
        .entity_get(&mut transaction, &key)
        .unwrap()
        .is_none());
    drop(transaction);
    assert_eq!(
        dispatchable_attempts(&mut database, 3_000).unwrap(),
        json!([])
    );
}

#[test]
fn dispatchable_index_and_recovery_settlement_commit_atomically_under_io_faults() {
    let seed = |vfs: std::sync::Arc<DeterministicVfs>| {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let accepted = accepted_executor_pair(&mut database, 11, "dispatch-fault", 1_000);
        vfs.arm_fault(None).unwrap();
        (
            database,
            accepted["attempt"]["attemptId"]
                .as_str()
                .unwrap()
                .to_owned(),
        )
    };
    let command = request(
        "turn.recover",
        11,
        json!({"user_id": 11, "created_before_ms": 2_000}),
        Some("dispatch-fault-recover"),
    );
    let baseline_vfs = prepared_vfs();
    let (mut baseline, _) = seed(baseline_vfs.clone());
    execute(&mut baseline, &command, 2_000).unwrap();
    let trace = baseline_vfs.trace().unwrap();
    drop(baseline);
    let faults = trace.iter().enumerate().flat_map(|(index, operation)| {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
        if operation == &Operation::Write {
            actions.push(FaultAction::ShortWrite(7));
        }
        actions.into_iter().map(move |action| FaultRule {
            operation_number: index as u64 + 1,
            action,
        })
    });
    for fault in faults {
        let vfs = prepared_vfs();
        let (mut database, attempt_id) = seed(vfs.clone());
        vfs.arm_fault(Some(fault)).unwrap();
        let _ = execute(&mut database, &command, 2_000);
        drop(database);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut reopened =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        reopened
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let read_attempt = request(
            "turn.attempt.get",
            11,
            json!({"user_id": 11, "attempt_id": attempt_id}),
            None,
        );
        let before = execute(&mut reopened, &read_attempt, 3_000).unwrap();
        let listed = listed_attempt_ids(&dispatchable_attempts(&mut reopened, 3_000).unwrap());
        match before["status"].as_str().unwrap() {
            "pending" => assert_eq!(listed, [attempt_id]),
            "interrupted" => assert!(listed.is_empty()),
            other => panic!("unexpected recovery status: {other}"),
        }
        // A lost acknowledgement may already have settled the Attempt;
        // replay must converge regardless of the reported recovered count.
        execute(&mut reopened, &command, 2_000).unwrap();
        assert_eq!(
            execute(&mut reopened, &read_attempt, 3_001).unwrap()["status"],
            "interrupted"
        );
        assert_eq!(
            dispatchable_attempts(&mut reopened, 3_000).unwrap(),
            json!([])
        );
    }
}
