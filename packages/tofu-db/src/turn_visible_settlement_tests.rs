//! Executable SQL/native parity for visible Flow phase terminal settlement.
//! Uses admitted semantic requests, temporary authorities, and deterministic VFS faults.
use super::*;
use crate::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
use crate::semantic::{admit_request, AuthenticatedScope};
use crate::semantic_executor::{execute_admitted_request, SemanticExecutionError};
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
use std::sync::Arc;

fn command(operation: &str, owner: u64, payload: Value, receipt: Option<&str>) -> Request {
    Request {
        correlation_id: [3; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: owner,
        tenant_id: Some(7),
        command_id: receipt.map(str::to_owned),
        schema_id: STORAGE_SCHEMA_VERSION,
        operation: operation.to_owned(),
        payload: serde_json::to_vec(&payload).unwrap(),
    }
}

fn execute(
    database: &mut AuthorityDatabase,
    request: &Request,
) -> Result<Value, SemanticExecutionError> {
    let negotiated = NegotiatedProtocol {
        protocol_version: PROTOCOL_VERSION,
        schema_id: STORAGE_SCHEMA_VERSION,
    };
    let scope = AuthenticatedScope {
        owner_id: request.owner_id,
        tenant_id: request.tenant_id,
    };
    let admitted = admit_request(request, negotiated, scope, 2_000).unwrap();
    let result = execute_admitted_request(database, admitted, 2_000)?;
    Ok(serde_json::from_slice(&result).unwrap())
}

fn read(database: &mut AuthorityDatabase, operation: &str, mut payload: Value) -> Value {
    payload["user_id"] = json!(11);
    execute(database, &command(operation, 11, payload, None)).unwrap()
}

struct Flow {
    root: String,
    attempt: String,
}

impl Flow {
    fn seed(database: &mut AuthorityDatabase) -> Self {
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            database,
            &command(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"phase-conversation", "user_id":11,
                    "command_id":"phase-pair", "input_projection":{"content":"goal"},
                    "conversation_defaults":{"allowCreate":true}, "now":1_000
                }),
                Some("phase-pair"),
            ),
        )
        .unwrap();
        let flow = Self {
            root: pair["turn"]["turnId"].as_str().unwrap().to_owned(),
            attempt: pair["attempt"]["attemptId"].as_str().unwrap().to_owned(),
        };
        execute(
            database,
            &command(
                "turn.attempt.bind",
                11,
                json!({
                    "attempt_id":flow.attempt, "task_id":"phase-task", "user_id":11
                }),
                Some("phase-bind"),
            ),
        )
        .unwrap();
        flow
    }

    fn sync(&self, live: bool, start: u64, messages: Value, receipt: &str) -> Request {
        command(
            "turn.visible.sync",
            11,
            json!({
                "conversation_id":"phase-conversation", "user_id":11,
                "attempt_id":self.attempt, "root_turn_id":self.root,
                "live":live, "start_index":start, "run_id":"phase-run",
                "messages":messages, "now":2_000
            }),
            Some(receipt),
        )
    }

    fn child(&self, index: u64) -> String {
        visible_turn_identity(&self.root, &self.attempt, index)
    }

    fn child_attempt(&self, index: u64) -> String {
        visible_attempt_identity(&self.attempt, index)
    }

    fn turn(&self, database: &mut AuthorityDatabase, index: u64) -> Value {
        read(
            database,
            "turn.get",
            json!({"conversation_id":"phase-conversation", "turn_id":self.child(index)}),
        )
    }

    fn events(&self, database: &mut AuthorityDatabase, index: u64) -> Value {
        read(
            database,
            "turn.events.list",
            json!({"attempt_id":self.child_attempt(index)}),
        )
    }

    fn snapshot(&self, database: &mut AuthorityDatabase) -> Value {
        json!({
            "turns":read(database, "turn.list", json!({"conversation_id":"phase-conversation"})),
            "revision":read(database, "turn.revision", json!({"conversation_id":"phase-conversation"})),
            "sync":read(database, "turn.sync.changes", json!({"conversation_id":"phase-conversation", "after":0})),
            "attempt":read(database, "turn.attempt.get", json!({"attempt_id":self.child_attempt(1)})),
            "events":self.events(database, 1),
            "outbox":database.logical_outbox_status(7, 11).unwrap().last_sequence,
        })
    }
}

#[test]
fn visible_settlement_verdict_matches_sql_evidence_precedence() {
    let cases = [
        (json!({}), "completed", "phase_completed"),
        (
            json!({"finishReason":null}),
            "failed",
            "completion_evidence_missing",
        ),
        (
            json!({"streamState":"provider_finished"}),
            "failed",
            "completion_evidence_missing",
        ),
        (
            json!({"streamState":null, "finish_reason":"stop"}),
            "completed",
            "provider_finished",
        ),
        (
            json!({"finishReason":"", "finish_reason":"stop"}),
            "failed",
            "completion_evidence_missing",
        ),
        (
            json!({"finishReason":"stop", "status":"error"}),
            "failed",
            "generation_error",
        ),
        (
            json!({"finishReason":"stop", "status":"aborted"}),
            "interrupted",
            "user_abort",
        ),
        (
            json!({"finishReason":"stop"}),
            "completed",
            "provider_finished",
        ),
        (
            json!({"finishReason":"stop", "streamState":"provider_finished"}),
            "completed",
            "provider_finished",
        ),
        (
            json!({"providerFinishReason":"tool_calls"}),
            "completed",
            "phase_completed",
        ),
        (
            json!({"finishReason":"error", "providerFinishReason":"stop"}),
            "failed",
            "generation_error",
        ),
        (
            json!({"finishReason":"stop", "error":{"message":"oops"}}),
            "failed",
            "generation_error",
        ),
        (
            json!({"finishReason":"stop", "error":"not a mapping"}),
            "failed",
            "generation_error",
        ),
        (
            json!({"aborted":true, "error":{"message":"oops"}}),
            "failed",
            "generation_error",
        ),
        (
            json!({"finishReason":"stop", "aborted":true, "error":{"message":"oops"}}),
            "interrupted",
            "user_abort",
        ),
        (
            json!({"finishReason":"interrupted"}),
            "interrupted",
            "user_abort",
        ),
        (
            json!({"finishReason":"stop", "streamState":"client_aborted"}),
            "interrupted",
            "user_abort",
        ),
        (
            json!({"finishReason":"stop", "aborted":true, "_abort_reason":"turn_attempt_stale_fence"}),
            "failed",
            "internal_fence_abort",
        ),
        (
            json!({"finishReason":"stop", "streamState":"bogus"}),
            "failed",
            "completion_evidence_missing",
        ),
        (
            json!({"finishReason":"stop", "stream_state":"premature_close"}),
            "failed",
            "provider_stream_error",
        ),
        (
            json!({"finishReason":"stop", "streamState":null, "stream_state":"premature_close"}),
            "failed",
            "provider_stream_error",
        ),
        (
            json!({"role":"user", "aborted":true}),
            "completed",
            "phase_completed",
        ),
        (
            json!({"role":"user", "_isFlowPlanner":true}),
            "completed",
            "phase_completed",
        ),
    ];
    for (message, status, cause) in cases {
        let (actual, settlement) = visible_settlement(message.as_object().unwrap(), false);
        assert_eq!(actual, status, "{message}");
        assert_eq!(settlement["cause"], cause, "{message}");
        assert_eq!(
            settlement["providerFinishReason"],
            message.get("finishReason").cloned().unwrap_or(Value::Null)
        );
        assert_eq!(
            settlement["error"],
            message.get("error").cloned().unwrap_or(Value::Null)
        );
        assert_eq!(settlement["outcome"], status);
        assert_eq!(settlement["resumeOptions"], json!([]));
        assert_eq!(
            visible_settlement(message.as_object().unwrap(), true),
            ("running", json!({}))
        );
    }
    for stream in [
        "premature_close",
        "malformed_stream",
        "semantic_progress_timeout",
        "no_actionable_output",
        "empty_response",
        "tool_payload_missing",
        "gateway_limit_response",
    ] {
        let message = json!({"finishReason":"stop", "streamState":stream});
        let (status, settlement) = visible_settlement(message.as_object().unwrap(), false);
        assert_eq!(status, "failed");
        assert_eq!(settlement["cause"], "provider_stream_error");
    }
    for finish in [
        "length",
        "max_tokens",
        "context_length",
        "content_filter",
        "budget_exceeded",
        "incomplete",
        "max_turns",
    ] {
        let message = json!({"finishReason":finish});
        let (status, settlement) = visible_settlement(message.as_object().unwrap(), false);
        assert_eq!(status, "truncated");
        assert_eq!(settlement["cause"], finish);
    }
}

#[test]
fn visible_settlement_live_and_imported_phases_are_terminal_and_replay_immutable() {
    for (evidence, expected) in [
        (
            json!({"finishReason":"stop", "streamState":"provider_finished"}),
            "completed",
        ),
        (json!({"error":{"message":"provider failed"}}), "failed"),
        (
            json!({"finishReason":"aborted", "aborted":true}),
            "interrupted",
        ),
        (json!({"finishReason":"length"}), "truncated"),
        (json!({"finishReason":null}), "failed"),
        (json!({}), "completed"),
        (json!({"role":"user", "_isFlowReview":true}), "completed"),
    ] {
        for was_live in [false, true] {
            let directory = tempfile::tempdir().unwrap();
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let flow = Flow::seed(&mut database);
            if was_live {
                execute(
                    &mut database,
                    &flow.sync(
                        true,
                        1,
                        json!([{"role":"assistant", "content":"draft", "_flowLivePhase":true}]),
                        "phase-live",
                    ),
                )
                .unwrap();
            }
            let mut message =
                json!({"role":"assistant", "content":"final result", "thinking":"reason"});
            message
                .as_object_mut()
                .unwrap()
                .extend(evidence.as_object().unwrap().clone());
            let result = execute(
                &mut database,
                &flow.sync(false, 1, json!([message]), "phase-settle"),
            )
            .unwrap();
            assert_eq!(result["visibleTurnIds"], json!([flow.root, flow.child(1)]));
            let child = flow.turn(&mut database, 1);
            assert_eq!(child["status"], expected, "{message} live={was_live}");
            assert_eq!(child["settlement"]["outcome"], expected);

            let terminal_text = child["projection"]["segments"]
                .as_array()
                .unwrap()
                .iter()
                .find(|segment| segment["type"] == "text" && segment["terminal"] == true)
                .expect("terminal phase has a deliverable text block");
            assert_eq!(
                terminal_text.get("resumable") == Some(&json!(true)),
                matches!(expected, "interrupted" | "truncated")
            );
            assert_eq!(child["projection"]["content"], "final result");
            assert_eq!(child["projectionRevision"], if was_live { 2 } else { 1 });
            assert_eq!(child["parentTurnId"], flow.root);
            let attempt = read(
                &mut database,
                "turn.attempt.get",
                json!({"attempt_id":flow.child_attempt(1)}),
            );
            assert_eq!(attempt["status"], expected);
            assert!(!attempt["settledAt"].is_null());
            let events = flow.events(&mut database, 1);
            assert_eq!(
                events.as_array().unwrap().len(),
                if was_live { 2 } else { 1 }
            );
            let terminal = events.as_array().unwrap().last().unwrap();
            assert_eq!(terminal["type"], "terminal_settlement");
            assert_eq!(terminal["payload"]["status"], expected);
            assert_eq!(
                read(
                    &mut database,
                    "turn.get",
                    json!({"conversation_id":"phase-conversation", "turn_id":flow.root})
                )["status"],
                "pending"
            );
            if was_live {
                assert!(child["projection"]["segments"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|segment| segment["text"] == "draft"));
            }
            let before = flow.snapshot(&mut database);
            let replay = execute(
                &mut database,
                &flow.sync(false, 1, json!([message]), "phase-exact-replay"),
            )
            .unwrap();
            assert_eq!(replay, result);
            assert_eq!(flow.snapshot(&mut database), before);
            for (receipt, live, update) in [
                ("reopen", true, message.clone()),
                ("rewrite", false, {
                    let mut m = message.clone();
                    m["content"] = json!("replacement");
                    m
                }),
                ("restatus", false, {
                    let mut m = message.clone();
                    m["aborted"] = json!(expected != "interrupted");
                    m["finishReason"] = json!("timeout");
                    m["role"] = json!("assistant");
                    m
                }),
            ] {
                assert!(
                    matches!(
                        execute(&mut database, &flow.sync(live, 1, json!([update]), receipt)),
                        Err(SemanticExecutionError::Conflict)
                    ),
                    "{receipt} {expected}"
                );
                assert_eq!(flow.snapshot(&mut database), before);
            }
        }
    }
}

#[test]
fn visible_settlement_late_conflict_rolls_back_prior_child_and_root_changes() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let flow = Flow::seed(&mut database);
    execute(
        &mut database,
        &flow.sync(
            true,
            1,
            json!([{"role":"assistant", "content":"first"}]),
            "first-live",
        ),
    )
    .unwrap();
    execute(
        &mut database,
        &flow.sync(
            true,
            2,
            json!([{"role":"assistant", "content":"second"}]),
            "second-live",
        ),
    )
    .unwrap();
    execute(
        &mut database,
        &flow.sync(
            false,
            2,
            json!([{"role":"assistant", "content":"second", "finishReason":"stop"}]),
            "second-settled",
        ),
    )
    .unwrap();
    let before = flow.snapshot(&mut database);
    let second = flow.turn(&mut database, 2);
    let second_events = flow.events(&mut database, 2);
    let batch = flow.sync(
        false,
        0,
        json!([
            {"role":"assistant", "content":"root replacement", "finishReason":"stop"},
            {"role":"assistant", "content":"first final", "finishReason":"stop"},
            {"role":"assistant", "content":"conflict", "finishReason":"aborted"}
        ]),
        "late-conflict",
    );
    assert!(matches!(
        execute(&mut database, &batch),
        Err(SemanticExecutionError::Conflict)
    ));
    assert_eq!(flow.snapshot(&mut database), before);
    assert_eq!(flow.turn(&mut database, 2), second);
    assert_eq!(flow.events(&mut database, 2), second_events);
    execute(
        &mut database,
        &flow.sync(
            false,
            1,
            json!([{"role":"assistant", "content":"first final", "finishReason":"stop"}]),
            "after-rollback",
        ),
    )
    .unwrap();
    assert_eq!(flow.turn(&mut database, 1)["status"], "completed");
    assert_eq!(flow.events(&mut database, 1).as_array().unwrap().len(), 2);
}

#[test]
fn visible_settlement_rejects_foreign_owner_and_stale_root() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let flow = Flow::seed(&mut database);
    execute(
        &mut database,
        &flow.sync(
            true,
            1,
            json!([{"role":"assistant", "content":"draft"}]),
            "live",
        ),
    )
    .unwrap();
    let before = flow.snapshot(&mut database);
    let mut foreign = flow.sync(
        false,
        1,
        json!([{"role":"assistant", "content":"foreign", "finishReason":"stop"}]),
        "foreign",
    );
    let mut payload: Value = serde_json::from_slice(&foreign.payload).unwrap();
    payload["user_id"] = json!(12);
    foreign.owner_id = 12;
    foreign.payload = serde_json::to_vec(&payload).unwrap();
    assert_eq!(execute(&mut database, &foreign).unwrap(), Value::Null);
    assert_eq!(flow.snapshot(&mut database), before);
    let mut foreign_tenant = flow.sync(
        false,
        1,
        json!([{"role":"assistant", "content":"foreign tenant", "finishReason":"stop"}]),
        "foreign-tenant",
    );
    foreign_tenant.tenant_id = Some(8);
    assert_eq!(
        execute(&mut database, &foreign_tenant).unwrap(),
        Value::Null
    );
    assert_eq!(flow.snapshot(&mut database), before);
    let mut stale = flow.sync(
        false,
        1,
        json!([{"role":"assistant", "content":"stale", "finishReason":"stop"}]),
        "stale",
    );
    let mut payload: Value = serde_json::from_slice(&stale.payload).unwrap();
    payload["root_turn_id"] = json!(flow.child(1));
    stale.payload = serde_json::to_vec(&payload).unwrap();
    assert_eq!(execute(&mut database, &stale).unwrap(), Value::Null);
    assert_eq!(flow.snapshot(&mut database), before);
}

fn fault_seed(vfs: Arc<DeterministicVfs>) -> (AuthorityDatabase, Flow, Request) {
    let mut database =
        AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone()).unwrap();
    let flow = Flow::seed(&mut database);
    execute(
        &mut database,
        &flow.sync(
            true,
            1,
            json!([{"role":"assistant", "content":"draft"}]),
            "live",
        ),
    )
    .unwrap();
    let command = flow.sync(
        false,
        1,
        json!([{"role":"assistant", "content":"final", "finishReason":"stop"}]),
        "fault-settle",
    );
    vfs.arm_fault(None).unwrap();
    (database, flow, command)
}

fn fault_vfs() -> Arc<DeterministicVfs> {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(std::path::Path::new("/data")).unwrap();
    vfs.sync_directory(std::path::Path::new("/")).unwrap();
    vfs
}

fn canonical_fault_snapshot(flow: &Flow, database: &mut AuthorityDatabase) -> Value {
    let snapshot = flow.snapshot(database);
    let mut encoded = serde_json::to_string(&snapshot).unwrap();
    for (index, turn) in snapshot["turns"].as_array().unwrap().iter().enumerate() {
        encoded = encoded.replace(turn["turnId"].as_str().unwrap(), &format!("turn-{index}"));
        if let Some(attempt) = turn["currentAttemptId"].as_str() {
            encoded = encoded.replace(attempt, &format!("attempt-{index}"));
        }
    }
    serde_json::from_str(&encoded).unwrap()
}

#[test]
fn visible_settlement_faults_recover_atomically_and_replay_once() {
    let baseline_vfs = fault_vfs();
    let (mut baseline, flow, command) = fault_seed(baseline_vfs.clone());
    execute(&mut baseline, &command).unwrap();
    let trace = baseline_vfs.trace().unwrap();
    let expected = canonical_fault_snapshot(&flow, &mut baseline);
    let mut faults = Vec::new();
    for (index, operation) in trace.iter().enumerate() {
        faults.push(FaultRule {
            operation_number: index as u64 + 1,
            action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
        });
        if operation == &Operation::Write {
            faults.push(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            });
        }
    }
    assert!(!faults.is_empty());
    eprintln!(
        "visible settlement fault cases: {} ({} authority operations)",
        faults.len(),
        trace.len()
    );
    for fault in faults {
        let vfs = fault_vfs();
        let (mut database, flow, command) = fault_seed(vfs.clone());
        let before = canonical_fault_snapshot(&flow, &mut database);
        vfs.arm_fault(Some(fault)).unwrap();
        let _ = execute(&mut database, &command);
        drop(database);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let state = canonical_fault_snapshot(&flow, &mut recovered);
        assert!(
            state == before || state == expected,
            "partial commit: {state}"
        );
        execute(&mut recovered, &command).unwrap();
        assert_eq!(canonical_fault_snapshot(&flow, &mut recovered), expected);
        execute(&mut recovered, &command).unwrap();
        assert_eq!(canonical_fault_snapshot(&flow, &mut recovered), expected);
    }
}

fn commit_fixture_mutation(
    database: &mut AuthorityDatabase,
    mut transaction: AuthorityTransaction,
) {
    database
        .logical_outbox_capture(
            &mut transaction,
            crate::logical_outbox::LogicalOutboxCapture {
                schema_version: STORAGE_SCHEMA_VERSION,
                registry_version: crate::generated_storage_operations::STORAGE_REGISTRY_VERSION,
                operation: "turn.event.record".to_owned(),
                request_id: "fence-fixture".to_owned(),
                request_digest: [8; 32],
                command_id: None,
                committed_at_ms: 2_000,
                clear_payload: b"test-only fenced authority fixture".to_vec(),
            },
        )
        .unwrap();
    database.commit(transaction).unwrap();
}

fn alter_turn(database: &mut AuthorityDatabase, turn_id: &str, field: &str, value: Value) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let key = turn_key(&transaction, "phase-conversation", turn_id).unwrap();
    let stored = database
        .entity_get(&mut transaction, &key)
        .unwrap()
        .unwrap();
    let mut document = materialize_turn(database, &mut transaction, &stored).unwrap();
    document.insert(field.to_owned(), value);
    let version = crate::versioned_document::stored_document_version(
        &stored,
        DOCUMENT_IDENTITY,
        &turn_logical_key("phase-conversation", turn_id),
    )
    .unwrap();
    crate::versioned_document::put(
        database,
        &mut transaction,
        crate::versioned_document::PutRequest {
            key,
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key("phase-conversation", turn_id),
            value_json: serde_json::to_vec(&document).unwrap(),
            expected_version: Some(version),
            updated_at_ms: 2_000,
        },
    )
    .unwrap();
    commit_fixture_mutation(database, transaction);
}

fn alter_attempt(database: &mut AuthorityDatabase, attempt_id: &str, field: &str, value: Value) {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let loaded = load_attempt_for_update(database, &mut transaction, attempt_id)
        .unwrap()
        .unwrap();
    let mut document = loaded.document.clone();
    document.insert(field.to_owned(), value);

    if field == "status" {
        document.insert("settledAt".to_owned(), json!(2_000));
    }
    store_attempt_document(
        database,
        &mut transaction,
        attempt_id,
        &loaded,
        &document,
        2_000,
    )
    .unwrap();
    commit_fixture_mutation(database, transaction);
}

#[test]
fn visible_settlement_rejects_superseded_child_attempt_and_mismatched_parent() {
    for mutation in [
        "currentAttemptId",
        "parentTurnId",
        "laneId",
        "flowRootAttemptId",
        "attemptStatus",
    ] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let flow = Flow::seed(&mut database);
        execute(
            &mut database,
            &flow.sync(
                true,
                1,
                json!([{"role":"assistant", "content":"draft"}]),
                "live",
            ),
        )
        .unwrap();
        match mutation {
            "flowRootAttemptId" => alter_attempt(
                &mut database,
                &flow.child_attempt(1),
                "_config",
                json!({"flowRootAttemptId":"another-root"}),
            ),
            "attemptStatus" => alter_attempt(
                &mut database,
                &flow.child_attempt(1),
                "status",
                json!("superseded"),
            ),
            field => alter_turn(
                &mut database,
                &flow.child(1),
                field,
                json!("another-identity"),
            ),
        }
        let before = flow.snapshot(&mut database);
        for live in [true, false] {
            let request = flow.sync(
                live,
                1,
                json!([{"role":"assistant", "content":"final", "finishReason":"stop"}]),
                if live { "live-replay" } else { "terminal" },
            );
            assert!(
                matches!(
                    execute(&mut database, &request),
                    Err(SemanticExecutionError::Conflict)
                ),
                "{mutation}"
            );
            assert_eq!(flow.snapshot(&mut database), before, "{mutation}");
        }
    }
}

#[test]
fn visible_settlement_stale_or_terminal_root_cannot_settle_child() {
    for superseded in [true, false] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let flow = Flow::seed(&mut database);
        execute(
            &mut database,
            &flow.sync(
                true,
                1,
                json!([{"role":"assistant", "content":"draft"}]),
                "live",
            ),
        )
        .unwrap();
        if superseded {
            alter_turn(
                &mut database,
                &flow.root,
                "currentAttemptId",
                json!("new-root-attempt"),
            );
        } else {
            execute(&mut database, &command("turn.event.record", 11, json!({
                "attempt_id":flow.attempt, "task_id":"phase-task", "terminal":true, "user_id":11,
                "status":"completed", "projection":{"content":"root final"},
                "settlement":{"outcome":"completed", "cause":"provider_finished", "resumeOptions":[]}, "now":2_000
            }), Some("root-settled"))).unwrap();
        }
        let before = flow.snapshot(&mut database);
        let request = flow.sync(
            false,
            1,
            json!([{"role":"assistant", "content":"final", "finishReason":"stop"}]),
            "terminal",
        );
        assert_eq!(execute(&mut database, &request).unwrap(), Value::Null);
        assert_eq!(flow.snapshot(&mut database), before);
        assert_eq!(flow.turn(&mut database, 1)["status"], "running");
    }
}

#[test]
fn visible_settlement_retires_live_projection_head_and_preserves_streamed_output() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let flow = Flow::seed(&mut database);
    execute(
        &mut database,
        &flow.sync(
            true,
            1,
            json!([{"role":"assistant", "content":"draft"}]),
            "live",
        ),
    )
    .unwrap();
    let content = "streamed evidence ".repeat(5_000);
    let checkpoint = command(
        "turn.event.record",
        11,
        json!({
            "attempt_id":flow.child_attempt(1), "flow_root_attempt_id":flow.attempt,
            "task_id":"phase-task", "user_id":11, "terminal":false, "now":2_000,
            "content":content, "slim":true
        }),
        Some("large-stream"),
    );
    assert_eq!(
        execute(&mut database, &checkpoint).unwrap()["applied"],
        true
    );
    let head = {
        let mut transaction = database.begin(7, 11).unwrap();
        let key = turn_key(&transaction, "phase-conversation", &flow.child(1)).unwrap();
        let stored = database
            .entity_get(&mut transaction, &key)
            .unwrap()
            .unwrap();
        let (_, physical) = crate::versioned_document::materialize_stored_document(
            &database,
            7,
            11,
            &stored,
            DOCUMENT_IDENTITY,
        )
        .unwrap();
        projection_head_from_document(&decode_turn_value(&physical).unwrap())
            .unwrap()
            .unwrap()
    };
    let message = json!({"role":"assistant", "finishReason":"timeout"});
    execute(
        &mut database,
        &flow.sync(false, 1, json!([message]), "settle-large"),
    )
    .unwrap();
    let child = flow.turn(&mut database, 1);
    assert_eq!(child["status"], "failed");
    assert_eq!(child["projection"]["content"], content);
    assert_eq!(child["projectionRevision"], 3);
    {
        let mut transaction = database.begin(7, 11).unwrap();
        let key = turn_key(&transaction, "phase-conversation", &flow.child(1)).unwrap();
        let stored = database
            .entity_get(&mut transaction, &key)
            .unwrap()
            .unwrap();
        let (_, physical) = crate::versioned_document::materialize_stored_document(
            &database,
            7,
            11,
            &stored,
            DOCUMENT_IDENTITY,
        )
        .unwrap();
        assert!(
            projection_head_from_document(&decode_turn_value(&physical).unwrap())
                .unwrap()
                .is_none()
        );
        let checkpoint = projection_checkpoint_key(
            &transaction,
            "phase-conversation",
            &flow.child(1),
            &head.head_id,
        )
        .unwrap();
        assert!(database
            .entity_get(&mut transaction, &checkpoint)
            .unwrap()
            .is_none());
    }
    let before = flow.snapshot(&mut database);
    execute(
        &mut database,
        &flow.sync(false, 1, json!([message]), "replay-large"),
    )
    .unwrap();
    assert_eq!(flow.snapshot(&mut database), before);
    // Same failed status, different cause: it is not an acknowledgement.
    assert!(matches!(
        execute(
            &mut database,
            &flow.sync(
                false,
                1,
                json!([{"role":"assistant", "finishReason":"premature_close"}]),
                "rewrite-cause"
            )
        ),
        Err(SemanticExecutionError::Conflict)
    ));
    assert_eq!(flow.snapshot(&mut database), before);
}

#[test]
fn visible_settlement_preserves_live_tools_and_appends_completion_evidence_once() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let flow = Flow::seed(&mut database);
    execute(
        &mut database,
        &flow.sync(
            true,
            1,
            json!([{
                "role":"assistant", "content":"draft", "toolRounds":[{
                    "toolCallId":"recorded", "toolName":"read_files", "roundNum":0,
                    "status":"done", "results":{"content":"durable live result"}
                }]
            }]),
            "tools-live",
        ),
    )
    .unwrap();
    let message = json!({
        "role":"assistant", "content":"final", "finishReason":"stop", "toolRounds":[
            {"toolCallId":"recorded", "toolName":"read_files", "roundNum":0, "results":{"content":"preview must not replace live evidence"}},
            {"toolCallId":"completion-only", "toolName":"read_files", "roundNum":1, "results":{"content":"new evidence"}}
        ]
    });
    execute(
        &mut database,
        &flow.sync(false, 1, json!([message]), "tools-settle"),
    )
    .unwrap();
    let child = flow.turn(&mut database, 1);
    assert_eq!(
        child["projection"]["toolRounds"].as_array().unwrap().len(),
        2
    );
    assert_eq!(
        child["projection"]["toolRounds"][0]["results"]["content"],
        "durable live result"
    );
    let segments = child["projection"]["segments"].as_array().unwrap();
    assert_eq!(
        segments
            .iter()
            .filter(|segment| segment["type"] == "tool_use")
            .count(),
        2
    );
    let before = flow.snapshot(&mut database);
    execute(
        &mut database,
        &flow.sync(false, 1, json!([message]), "tools-replay"),
    )
    .unwrap();
    assert_eq!(flow.snapshot(&mut database), before);
}

#[test]
fn visible_tool_metadata_is_typed_and_retained_after_public_sync_and_replay() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let flow = Flow::seed(&mut database);
    let request = flow.sync(true, 1, json!([{
        "role":"assistant", "content":"working", "toolRounds":[
            {"toolCallId":"early", "toolName":"run_command", "roundNum":0,
             "toolModelContent":"model evidence"},
            {"toolCallId":"finished", "toolName":"read_files", "roundNum":7,
             "toolContent":"display evidence", "status":"done"}
        ]
    }]), "typed-tool-metadata");
    execute(&mut database, &request).unwrap();
    let child = flow.turn(&mut database, 1);
    let tools: Vec<_> = child["projection"]["segments"].as_array().unwrap().iter()
        .filter(|segment| segment["type"] == "tool_use").collect();
    assert_eq!(tools.len(), 2);
    assert_eq!(tools[0]["roundNum"], 0);
    assert!(tools[0]["result"].get("status").is_none());
    assert_eq!(tools[0]["result"]["modelContent"], "model evidence");
    assert_eq!(tools[1]["roundNum"], 7);
    assert_eq!(tools[1]["result"]["status"], "done");
    assert_eq!(tools[1]["result"]["content"], "display evidence");
    let before = flow.snapshot(&mut database);
    execute(&mut database, &request).unwrap();
    assert_eq!(flow.snapshot(&mut database), before);
}
