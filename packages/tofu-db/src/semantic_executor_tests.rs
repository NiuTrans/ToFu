    #[test]
    fn large_agent_checkpoint_uses_blob_budget_and_compact_outbox() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, &request("swarm.session.save", 11, json!({
            "swarm_key":"large-semantic", "specs":[], "config":{"user_id":11}, "now_ms":100
        }), Some("large-session")), 1_000).unwrap();
        let content = "界".repeat(3 * 1024 * 1024);
        let checkpoint = request("swarm.agent.save", 11, json!({
            "swarm_key":"large-semantic", "agent_id":"worker", "status":"completed",
            "messages":[{"role":"tool", "content":content}],
            "result":{"final_answer":"done"}, "delivered":false, "now_ms":200
        }), Some("large-checkpoint"));
        assert!(checkpoint.payload.len() > crate::protocol::MAX_PAYLOAD_BYTES);
        let saved = execute(&mut database, &checkpoint, 2_000).unwrap();
        assert_eq!(execute(&mut database, &checkpoint, 2_001).unwrap(), saved);
        let mut divergent = checkpoint.clone();
        let mut value: Value = serde_json::from_slice(&divergent.payload).unwrap();
        value["result"]["final_answer"] = json!("different");
        divergent.payload = serde_json::to_vec(&value).unwrap();
        assert!(execute(&mut database, &divergent, 2_002).is_err());
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let query = request("swarm.session.get", 11, json!({"swarm_key":"large-semantic"}), None);
        let recovered = execute(&mut database, &query, 3_000).unwrap();
        assert_eq!(recovered["agents"][0]["messages"][0]["content"], content);
        let other = request("swarm.session.get", 12, json!({"swarm_key":"large-semantic"}), None);
        assert!(execute(&mut database, &other, 3_001).unwrap().is_null());
    }

    use super::*;
    use crate::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
    use crate::semantic::{admit_request, AuthenticatedScope};
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

    fn request(
        operation: &str,
        owner_id: u64,
        payload: Value,
        command_id: Option<&str>,
    ) -> Request {
        Request {
            correlation_id: [3; 16],
            deadline_unix_ms: 10_000_000_000,
            owner_id,
            tenant_id: Some(7),
            command_id: command_id.map(str::to_owned),
            schema_id: STORAGE_SCHEMA_VERSION,
            operation: operation.to_owned(),
            payload: serde_json::to_vec(&payload).unwrap(),
        }
    }

    fn execute(
        database: &mut AuthorityDatabase,
        request: &Request,
        now_unix_ms: u64,
    ) -> Result<Value, SemanticExecutionError> {
        let negotiated = NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        };
        let scope = AuthenticatedScope {
            owner_id: request.owner_id,
            tenant_id: request.tenant_id,
        };
        let admitted = if request.payload.len() > crate::generated_storage_v2::MAX_PAYLOAD_BYTES {
            crate::semantic::admit_streamed_request(
                request,
                negotiated,
                scope,
                now_unix_ms,
                crate::generated_storage_v2::MAX_STREAMED_REQUEST_PAYLOAD_BYTES,
            )
        } else {
            admit_request(request, negotiated, scope, now_unix_ms)
        }
        .unwrap();
        let response = execute_admitted_request(database, admitted, now_unix_ms)?;
        serde_json::from_slice(&response).map_err(|_| {
            SemanticExecutionError::Integrity("test response is not valid JSON".to_owned())
        })
    }

    #[test]
    fn turn_cleanup_is_bounded_owner_scoped_receipted_and_reference_safe() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // A superseded Attempt that is still its Turn's current attempt is
        // referenced and must survive every cleanup round.
        let seeded = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "cleanup-conversation",
                    "user_id": 11,
                    "command_id": "cleanup-seed",
                    "actor": "assistant",
                    "kind": "reply",
                    "status": "superseded",
                    "projection": {"content": "stale answer"},
                    "settlement": {"outcome": "superseded", "cause": "regenerate", "resumeOptions": []},
                    "turn_id": "cleanup-turn",
                    "conversation_defaults": {"allowCreate": true, "title": "Cleanup"},
                    "now": 1_000
                }),
                Some("cleanup-seed-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let superseded_attempt_id = seeded["attempt"]["attemptId"].as_str().unwrap().to_owned();
        let cleanup_payload =
            |retention_ms: u64| json!({"user_id": 11, "retention_ms": retention_ms, "limit": 8});

        // The retention fence rejects candidates newer than the cutoff before
        // any deletion is considered.
        let retained = execute(
            &mut database,
            &request(
                "turn.cleanup",
                11,
                cleanup_payload(10_000),
                Some("cleanup-retained"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(retained["deletedAttempts"], 0);
        assert_eq!(retained["moreCandidates"], false);
        // An aged-out candidate is still fenced while a live Turn references
        // it as its current attempt.
        let referenced = execute(
            &mut database,
            &request(
                "turn.cleanup",
                11,
                cleanup_payload(0),
                Some("cleanup-referenced"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(referenced["deletedAttempts"], 0);
        assert_eq!(referenced["skippedAttempts"], 1);

        // Regeneration installs a fresh current Attempt; the superseded one
        // is no longer referenced and becomes eligible.
        let regenerated = execute(
            &mut database,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "cleanup-conversation",
                    "turn_id": "cleanup-turn",
                    "user_id": 11,
                    "command_id": "cleanup-regenerate",
                    "operation": "regenerate",
                    "target_actor": "assistant",
                    "target_kind": "reply",
                    "expected_projection_revision": 1,
                    "config": {"model": "cleanup-model"}
                }),
                Some("cleanup-regenerate-receipt"),
            ),
            3_000,
        )
        .unwrap();
        let current_attempt_id = regenerated["attempt"]["attemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_ne!(current_attempt_id, superseded_attempt_id);

        let cleanup_request = request(
            "turn.cleanup",
            11,
            cleanup_payload(0),
            Some("cleanup-round-1"),
        );
        let cleaned = execute(&mut database, &cleanup_request, 4_000).unwrap();
        assert_eq!(cleaned["deletedAttempts"], 1);
        assert_eq!(cleaned["deletedAttemptIds"], json!([superseded_attempt_id]));
        assert_eq!(cleaned["skippedAttempts"], 0);
        assert_eq!(cleaned["moreCandidates"], false);
        assert_eq!(cleaned["backend"], "tofu-db");
        // The stored receipt makes the lost-ACK replay exact instead of
        // re-executing the bounded deletion.
        assert_eq!(
            execute(&mut database, &cleanup_request, 9_000).unwrap(),
            cleaned
        );
        let drained = execute(
            &mut database,
            &request(
                "turn.cleanup",
                11,
                cleanup_payload(0),
                Some("cleanup-round-2"),
            ),
            5_000,
        )
        .unwrap();
        assert_eq!(drained["deletedAttempts"], 0);
        assert_eq!(drained["scannedIndexEntries"], 0);

        // The Turn and its live Attempt are untouched; the deleted Attempt is
        // gone while its tenant-global identity claim stays fenced.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.attempt.get",
                    11,
                    json!({"attempt_id": superseded_attempt_id, "user_id": 11}),
                    None,
                ),
                5_001,
            )
            .unwrap(),
            Value::Null,
        );
        let turn = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "cleanup-conversation",
                    "turn_id": "cleanup-turn",
                    "user_id": 11
                }),
                None,
            ),
            5_002,
        )
        .unwrap();
        assert_eq!(turn["currentAttemptId"], json!(current_attempt_id));
        assert_eq!(turn["status"], "pending");

        // Owner scoping and payload bounds fail before any deletion.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.cleanup",
                    11,
                    json!({"user_id": 12, "retention_ms": 0}),
                    Some("cleanup-foreign-payload"),
                ),
                6_000,
            )
            .unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch,
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.cleanup",
                    11,
                    json!({"user_id": 11, "limit": 5000}),
                    Some("cleanup-too-wide"),
                ),
                6_000,
            )
            .unwrap_err(),
            SemanticExecutionError::InvalidPayload,
        );
        let foreign = execute(
            &mut database,
            &request(
                "turn.cleanup",
                12,
                json!({"user_id": 12, "retention_ms": 0}),
                Some("cleanup-other-owner"),
            ),
            6_001,
        )
        .unwrap();
        assert_eq!(foreign["deletedAttempts"], 0);
        assert_eq!(foreign["scannedIndexEntries"], 0);
    }

    fn seeded_cleanup_scenario(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String, String, Request) {
        let mut database = simulated_database(vfs);
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "cleanup-fault-conversation",
                    "user_id": 11,
                    "command_id": "cleanup-fault-pair",
                    "input_projection": {"content": "question"},
                    "conversation_defaults": {"allowCreate": true},
                    "now": 1_000
                }),
                Some("cleanup-fault-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let superseded_attempt_id = pair["attempt"]["attemptId"].as_str().unwrap().to_owned();
        let turn_id = pair["turn"]["turnId"].as_str().unwrap().to_owned();
        let settled = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": superseded_attempt_id,
                    "user_id": 11,
                    "terminal": true,
                    "status": "superseded",
                    "settlement": {"outcome": "superseded", "cause": "regenerate", "resumeOptions": []},
                    "now": 2_000
                }),
                Some("cleanup-fault-settle"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(settled["applied"], true);
        let regenerated = execute(
            &mut database,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "cleanup-fault-conversation",
                    "turn_id": turn_id,
                    "user_id": 11,
                    "command_id": "cleanup-fault-regenerate",
                    "operation": "regenerate",
                    "target_actor": "assistant",
                    "target_kind": "reply",
                    "expected_projection_revision": 2,
                    "config": {"model": "cleanup-fault-model"}
                }),
                Some("cleanup-fault-regenerate-receipt"),
            ),
            3_000,
        )
        .unwrap();
        let current_attempt_id = regenerated["attempt"]["attemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        // The terminal settlement's replay envelope fences the superseded
        // Attempt through an exact sync reference; only expiring that replay
        // data releases the fence and makes cleanup eligible.
        let pruned = execute(
            &mut database,
            &request(
                "turn.sync.prune",
                11,
                json!({"user_id": 11, "created_before_ms": 4_000, "max_rows": 512}),
                Some("cleanup-fault-sync-prune"),
            ),
            3_500,
        )
        .unwrap();
        assert!(pruned["deletedRows"].as_u64().unwrap() >= 1);
        let command = request(
            "turn.cleanup",
            11,
            json!({"user_id": 11, "retention_ms": 0, "limit": 8}),
            Some("cleanup-fault-round"),
        );
        (database, superseded_attempt_id, current_attempt_id, command)
    }

    fn assert_turn_cleanup_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
        superseded_attempt_id: &str,
        current_attempt_id: &str,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        // Whether the faulted commit landed or not, replay converges to one
        // complete cleanup: either the stored receipt or one re-execution.
        let replayed = execute(&mut recovered, command, 4_000).unwrap();
        assert_eq!(replayed["deletedAttempts"], 1);
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "turn.attempt.get",
                    11,
                    json!({"attempt_id": superseded_attempt_id, "user_id": 11}),
                    None,
                ),
                4_001,
            )
            .unwrap(),
            Value::Null,
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "turn.attempt.get",
                    11,
                    json!({"attempt_id": current_attempt_id, "user_id": 11}),
                    None,
                ),
                4_002,
            )
            .unwrap()["attemptId"],
            json!(current_attempt_id),
        );
        let drained = execute(
            &mut recovered,
            &request(
                "turn.cleanup",
                11,
                json!({"user_id": 11, "retention_ms": 0, "limit": 8}),
                Some("cleanup-fault-drain"),
            ),
            4_003,
        )
        .unwrap();
        assert_eq!(drained["deletedAttempts"], 0);
    }

    #[test]
    fn every_turn_cleanup_commit_fault_recovers_attempt_and_turn_atomically() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, _, _, command) = seeded_cleanup_scenario(baseline_vfs.clone());
        execute(&mut baseline, &command, 4_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, superseded_attempt_id, current_attempt_id, command) =
                seeded_cleanup_scenario(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_turn_cleanup_replay_converges(
                vfs,
                &command,
                &superseded_attempt_id,
                &current_attempt_id,
            );
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, superseded_attempt_id, current_attempt_id, command) =
                seeded_cleanup_scenario(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_turn_cleanup_replay_converges(
                vfs,
                &command,
                &superseded_attempt_id,
                &current_attempt_id,
            );
        }
    }

    #[test]
    fn turn_search_backfill_rejects_owner_scoped_payloads_without_authority_io() {
        // A global rebuild must never be disguised as, or narrowed into, an
        // authenticated-owner rebuild: any payload identity fails closed
        // before touching storage.
        let vfs = std::sync::Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        vfs.arm_fault(None).unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.search.backfill",
                    11,
                    json!({"user_id": 13, "cursor": "", "max_rows": 8}),
                    Some("backfill-foreign-owner"),
                ),
                1_000,
            ),
            Err(SemanticExecutionError::AuthorityScopeMismatch),
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.search.backfill",
                    11,
                    json!({"tenant_id": 8}),
                    Some("backfill-foreign-tenant"),
                ),
                1_000,
            ),
            Err(SemanticExecutionError::AuthorityScopeMismatch),
        );
        for payload in [
            json!({"user_id": 11}),
            json!({"tenant_id": 7}),
            json!({"cursor": "x".repeat(1025)}),
            json!({"max_rows": 0}),
            json!({"max_rows": 65}),
            json!({"max_bytes": 0}),
        ] {
            assert_eq!(
                execute(
                    &mut database,
                    &request(
                        "turn.search.backfill",
                        11,
                        payload,
                        Some("backfill-invalid")
                    ),
                    1_000,
                ),
                Err(SemanticExecutionError::InvalidPayload),
            );
        }
        assert!(
            vfs.trace().unwrap().is_empty(),
            "rejected backfill payloads performed authority IO"
        );
    }

    #[test]
    fn turn_search_backfill_enumerates_owner_scopes_beyond_the_caller() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // Seed tenant-global conversation claims for two owners; the caller
        // authenticates as owner 11 only.
        let mut seed = database
            .begin_with_identity_claim_scopes(7, crate::conversation_header::TENANT_GLOBAL_OWNER_ID)
            .unwrap();
        for (conversation_id, owner) in [("c00", 11u64), ("c01", 11), ("c02", 12)] {
            database
                .entity_put(
                    &mut seed,
                    EntityKey::new(
                        7,
                        crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
                        crate::generated_tofudb_ir::CONVERSATION_ID_CLAIM_NAMESPACE,
                        conversation_id.as_bytes(),
                    )
                    .unwrap(),
                    owner.to_be_bytes().to_vec(),
                )
                .unwrap();
        }
        database.commit(seed).unwrap();
        let first = execute(
            &mut database,
            &request(
                "turn.search.backfill",
                11,
                json!({"cursor": "", "max_rows": 2, "max_bytes": 2_000_000}),
                Some("backfill-round-1"),
            ),
            1_000,
        )
        .unwrap();
        assert_eq!(first["scanned"], 2);
        assert_eq!(first["nextCursor"], "c01");
        assert_eq!(first["remaining"], true);
        let second = execute(
            &mut database,
            &request(
                "turn.search.backfill",
                11,
                json!({"cursor": "c01", "max_rows": 2, "max_bytes": 2_000_000}),
                Some("backfill-round-2"),
            ),
            1_001,
        )
        .unwrap();
        assert_eq!(second["scanned"], 1);
        assert_eq!(second["remaining"], false);
        // The sweep scheduled a real rebuild for owner 12 even though the
        // authenticated caller is owner 11: global semantics, not an
        // authenticated-owner rebuild.
        let owner12 = search_backfill::load(&database, &mut database.begin(7, 12).unwrap())
            .unwrap()
            .unwrap();
        assert_eq!(owner12.generation, 1);
        assert!(!owner12.complete);
        assert!(
            search_backfill::load(&database, &mut database.begin(7, 11).unwrap())
                .unwrap()
                .is_some()
        );
        assert!(
            search_backfill::load(&database, &mut database.begin(8, 11).unwrap())
                .unwrap()
                .is_none()
        );
        // The command is receipt-free: a lost-ACK retry with the consumed
        // cursor re-executes as a no-op reporting the durable sweep position.
        let retry = execute(
            &mut database,
            &request(
                "turn.search.backfill",
                11,
                json!({"cursor": "c01", "max_rows": 2, "max_bytes": 2_000_000}),
                Some("backfill-round-2-retry"),
            ),
            1_002,
        )
        .unwrap();
        assert_eq!(retry["scanned"], 0);
        assert_eq!(retry["nextCursor"], "c02");
        assert_eq!(retry["remaining"], false);
        // A cursor from the sweep's future fails closed.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.search.backfill",
                    11,
                    json!({"cursor": "c99"}),
                    Some("backfill-stale"),
                ),
                1_003,
            ),
            Err(SemanticExecutionError::InvalidPayload),
        );
    }

    #[test]
    fn turn_create_pair_is_atomic_queue_aware_and_command_replayable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let payload = json!({
            "conversation_id": "pair-conversation",
            "user_id": 11,
            "command_id": "pair-command",
            "input_projection": {"content": "hello"},
            "config": {"model": "test"},
            "conversation_defaults": {"allowCreate": true, "title": "Pair"},
            "now": 1_000
        });
        let first = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                payload.clone(),
                Some("pair-receipt-1"),
            ),
            1_000,
        )
        .unwrap();
        assert_eq!(first["submittedTurn"]["projection"]["content"], "hello");
        assert_eq!(first["submittedTurn"]["ordinal"], 0);
        assert_eq!(first["turn"]["ordinal"], 1);
        assert_eq!(
            first["turn"]["parentTurnId"],
            first["submittedTurn"]["turnId"]
        );
        assert_eq!(first["attempt"]["queueBinding"], Value::Null);
        assert_eq!(first["conversationRevision"], 1);
        assert_eq!(first["streamCursor"], 1);
        assert_eq!(first["_needsStart"], true);
        assert_eq!(first["idempotentReplay"], false);

        let replay = execute(
            &mut database,
            &request("turn.create_pair", 11, payload, Some("pair-receipt-2")),
            1_001,
        )
        .unwrap();
        assert_eq!(replay["turn"], first["turn"]);
        assert_eq!(replay["attempt"], first["attempt"]);
        assert_eq!(replay["conversationRevision"], 1);
        assert_eq!(replay["idempotentReplay"], true);

        let queued = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "queued-pair",
                    "user_id": 11,
                    "command_id": "queued-pair-command",
                    "input_projection": "queued hello",
                    "dispatch_mode": "conversation_executor",
                    "queue_binding": {"queueId": "queue-pair", "kind": "real", "priority": 7},
                    "conversation_defaults": {"allowCreate": true},
                    "now": 2_000
                }),
                Some("queued-pair-receipt"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(queued["queued"], true);
        assert_eq!(queued["queueId"], "queue-pair");
        assert_eq!(queued["position"], 1);
        assert_eq!(queued["_needsStart"], false);
        assert_eq!(queued["attempt"]["queueBinding"]["queueId"], "queue-pair");
        assert_eq!(
            queued["queueItem"]["attemptId"],
            queued["attempt"]["attemptId"]
        );

        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id": "queued-pair", "user_id": 11, "after": 0}),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(changes["events"].as_array().unwrap().len(), 1);
        assert_eq!(
            changes["events"][0]["payload"]["turns"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(
            changes["events"][0]["payload"]["queueItemUpserts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );

        let activate_request = request(
            "turn.queue.activate",
            11,
            json!({
                "conversation_id": "queued-pair",
                "queue_id": "queue-pair",
                "user_id": 11
            }),
            Some("queue-pair-activate"),
        );
        let activated = execute(&mut database, &activate_request, 2_010).unwrap();
        assert_eq!(activated["queued"], false);
        assert_eq!(activated["queueId"], "queue-pair");
        assert_eq!(activated["_needsStart"], true);
        assert_eq!(activated["conversationRevision"], 2);
        assert_eq!(activated["attempt"]["queueBinding"], Value::Null);
        // `turn.queue.activate` is receipt-free per the canonical contract
        // (`contracts/storage_operations_v1.json`), matching the legacy
        // SQLite authority: a replayed activation re-executes the handler
        // and the retired queue row yields `database_not_found`.
        assert_eq!(
            execute(&mut database, &activate_request, 2_011).unwrap_err(),
            SemanticExecutionError::NotFound
        );
        let queue_after_activate = execute(
            &mut database,
            &request(
                "queue.list",
                11,
                json!({"conv_id": "queued-pair", "user_id": 11}),
                None,
            ),
            2_012,
        )
        .unwrap();
        assert_eq!(queue_after_activate, json!([]));
        let dispatchable = execute(
            &mut database,
            &request(
                "turn.attempt.dispatchable.list",
                11,
                json!({"user_id": 11, "created_before_ms": 3_000, "limit": 32}),
                None,
            ),
            2_012,
        )
        .unwrap();
        assert!(dispatchable
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["attempt"].get("attemptId") == activated["attempt"].get("attemptId")));
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id": "queued-pair", "user_id": 11, "after": 1}),
                None,
            ),
            2_013,
        )
        .unwrap();
        assert_eq!(
            changes["events"][0]["payload"]["removedQueueIds"],
            json!(["queue-pair"])
        );

        execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "activate-conflict",
                    "user_id": 11,
                    "command_id": "activate-conflict-live",
                    "conversation_defaults": {"allowCreate": true},
                    "now": 2_014
                }),
                Some("activate-conflict-live-create"),
            ),
            2_014,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "activate-conflict",
                    "user_id": 11,
                    "command_id": "activate-conflict-queued",
                    "queue_binding": {"queueId": "activate-conflict-queue"},
                    "now": 2_015
                }),
                Some("activate-conflict-queued-create"),
            ),
            2_015,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.queue.activate",
                    11,
                    json!({
                        "conversation_id":"activate-conflict",
                        "queue_id":"activate-conflict-queue",
                        "user_id":11
                    }),
                    Some("activate-conflict-command"),
                ),
                2_016,
            )
            .unwrap_err(),
            SemanticExecutionError::TurnInProgress
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "queue.list",
                    11,
                    json!({"conv_id":"activate-conflict", "user_id":11}),
                    None,
                ),
                2_017,
            )
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
            1
        );

        let cancellable = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "cancelled-pair",
                    "user_id": 11,
                    "command_id": "cancelled-pair-command",
                    "input_projection": "cancel me",
                    "queue_binding": {"queueId": "cancelled-pair-queue"},
                    "conversation_defaults": {"allowCreate": true},
                    "now": 2_020
                }),
                Some("cancelled-pair-create"),
            ),
            2_020,
        )
        .unwrap();
        let cancel_request = request(
            "turn.queue.cancel",
            11,
            json!({
                "conversation_id": "cancelled-pair",
                "queue_id": "cancelled-pair-queue",
                "user_id": 11
            }),
            Some("cancelled-pair-cancel"),
        );
        let cancelled = execute(&mut database, &cancel_request, 2_030).unwrap();
        assert_eq!(cancelled["cancelled"], true);
        assert_eq!(cancelled["conversationRevision"], 2);
        assert_eq!(cancelled["inputTurn"], cancellable["submittedTurn"]);
        assert_eq!(cancelled["deletedTurnIds"].as_array().unwrap().len(), 2);
        // `turn.queue.cancel` is likewise receipt-free: a replay re-executes
        // the handler, and the legacy authority answers the missing queue row
        // with the not-cancelled idempotent shape at the current revision
        // instead of the stored first response.
        let cancel_replay = execute(&mut database, &cancel_request, 2_031).unwrap();
        assert_eq!(cancel_replay["conversationId"], "cancelled-pair");
        assert_eq!(cancel_replay["conversationRevision"], 2);
        assert_eq!(cancel_replay["queueId"], "cancelled-pair-queue");
        assert_eq!(cancel_replay["cancelled"], false);
        assert_eq!(cancel_replay["inputTurn"], Value::Null);
        assert_eq!(cancel_replay["deletedTurnIds"], json!([]));
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.list",
                    11,
                    json!({"conversation_id":"cancelled-pair", "user_id":11}),
                    None,
                ),
                2_032,
            )
            .unwrap(),
            json!([])
        );

        let invalid_parent = request(
            "turn.create_pair",
            11,
            json!({
                "conversation_id": "queued-pair", "user_id": 11,
                "command_id": "invalid-parent-command",
                "parent_turn_id": "missing-parent",
                "queue_binding": {"queueId": "invalid-parent-queue"}
            }),
            Some("invalid-parent-receipt"),
        );
        assert_eq!(
            execute(&mut database, &invalid_parent, 2_100).unwrap_err(),
            SemanticExecutionError::TurnParentInvalid
        );
        let stale_tail = request(
            "turn.create_pair",
            11,
            json!({
                "conversation_id": "queued-pair", "user_id": 11,
                "command_id": "stale-tail-command",
                "parent_turn_id": queued["submittedTurn"]["turnId"],
                "require_parent_is_lane_tail": true,
                "queue_binding": {"queueId": "stale-tail-queue"}
            }),
            Some("stale-tail-receipt"),
        );
        assert_eq!(
            execute(&mut database, &stale_tail, 2_200).unwrap_err(),
            SemanticExecutionError::TurnLaneAdvanced
        );
        execute(
            &mut database,
            &request(
                "queue.enqueue",
                11,
                json!({
                    "conv_id": "queued-pair",
                    "queue_id": "queued-pair-human-blocker",
                    "kind": "real",
                    "message": {"text": "new human message"},
                    "config": {},
                    "created_at_ms": 2_250,
                    "user_id": 11
                }),
                Some("queued-pair-human-blocker-enqueue"),
            ),
            2_250,
        )
        .unwrap();
        let superseded = request(
            "turn.create_pair",
            11,
            json!({
                "conversation_id": "queued-pair", "user_id": 11,
                "command_id": "superseded-command",
                "input_actor": "virtual_user",
                "reject_if_human_queued": true
            }),
            Some("superseded-receipt"),
        );
        assert_eq!(
            execute(&mut database, &superseded, 2_300).unwrap_err(),
            SemanticExecutionError::TurnSupersededByHuman
        );
    }

    #[test]
    fn turn_steer_commit_fences_the_live_window_and_replays_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "steer-conversation",
                    "user_id": 11,
                    "command_id": "steer-pair",
                    "input_projection": {"content": "question"},
                    "conversation_defaults": {"allowCreate": true},
                    "now": 1_000
                }),
                Some("steer-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.steer.commit",
                    11,
                    json!({
                        "conversation_id":"steer-conversation",
                        "attempt_id":attempt_id,
                        "command_id":"too-early",
                        "text":"task binding is required",
                        "user_id":11
                    }),
                    Some("too-early-steer"),
                ),
                1_005,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id, "task_id":"steer-task", "user_id":11}),
                Some("steer-bind"),
            ),
            1_010,
        )
        .unwrap();
        let long_text = "界".repeat(1_201);
        let steer_request = request(
            "turn.steer.commit",
            11,
            json!({
                "conversation_id":"steer-conversation",
                "attempt_id":attempt_id,
                "command_id":"operator-command",
                "text":long_text,
                "user_id":11
            }),
            Some("steer-receipt"),
        );
        let steered = execute(&mut database, &steer_request, 1_020).unwrap();
        assert_eq!(steered["steered"], true);
        assert_eq!(steered["injectionId"], "operator-command");
        assert_eq!(steered["blockId"], "injection:user-steer:operator-command");
        assert_eq!(steered["conversationId"], "steer-conversation");
        assert_eq!(steered["conversationRevision"], 3);
        assert!(steered.get("turn").is_none());
        let live_projection = |database: &mut AuthorityDatabase, at: u64| {
            let turns = execute(
                database,
                &request(
                    "turn.list",
                    11,
                    json!({"conversation_id":"steer-conversation", "user_id":11}),
                    None,
                ),
                at,
            )
            .unwrap();
            let output = turns.as_array().unwrap().last().unwrap();
            assert_eq!(output["turnId"], steered["turnId"]);
            output.clone()
        };
        let output = live_projection(&mut database, 1_021);
        assert_eq!(output["projectionRevision"], 3);
        let preview = output["projection"]["_userSteerInjects"][0]["previews"][0]["text"]
            .as_str()
            .unwrap();
        assert_eq!(preview.chars().count(), 1_200);
        assert!(preview.chars().all(|character| character == '界'));
        assert_eq!(
            execute(&mut database, &steer_request, 1_022).unwrap(),
            steered
        );

        let deduplicated = execute(
            &mut database,
            &request(
                "turn.steer.commit",
                11,
                json!({
                    "conversation_id":"steer-conversation",
                    "attempt_id":attempt_id,
                    "command_id":"operator-command",
                    "text":"ignored duplicate text",
                    "user_id":11
                }),
                Some("different-steer-receipt"),
            ),
            1_030,
        )
        .unwrap();
        assert_eq!(deduplicated["conversationRevision"], 4);
        assert!(deduplicated.get("turn").is_none());
        let output = live_projection(&mut database, 1_031);
        assert_eq!(output["projectionRevision"], 4);
        assert_eq!(
            output["projection"]["_userSteerInjects"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id":"steer-conversation", "user_id":11, "after":0}),
                None,
            ),
            1_031,
        )
        .unwrap();
        assert_eq!(changes["events"].as_array().unwrap().len(), 4);
        assert_eq!(changes["events"][2]["type"], "conversation.activity");
        assert_eq!(changes["events"][2]["turnId"], steered["turnId"]);
        assert_eq!(changes["events"][2]["payload"]["requiresSnapshot"], true);
        assert_eq!(changes["events"][2]["payload"]["conversationRevision"], 3);
        assert_eq!(changes["events"][3]["type"], "conversation.activity");

        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.steer.commit",
                    12,
                    json!({
                        "conversation_id":"steer-conversation",
                        "attempt_id":attempt_id,
                        "command_id":"foreign-command",
                        "text":"must not cross owners",
                        "user_id":12
                    }),
                    Some("foreign-steer-receipt"),
                ),
                1_040,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
    }

    #[test]
    fn turn_related_announce_bridges_a_large_live_projection_atomically() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"related-conversation",
                    "user_id":11,
                    "command_id":"related-pair",
                    "input_projection":{"content":"question"},
                    "conversation_defaults":{"allowCreate":true},
                    "now":1_000
                }),
                Some("related-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let root_turn_id = pair["turn"]["turnId"].as_str().unwrap();
        let input_turn_id = pair["submittedTurn"]["turnId"].as_str().unwrap();
        let initial_projection_revision = pair["turn"]["projectionRevision"].as_u64().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id, "task_id":"related-task", "user_id":11}),
                Some("related-bind"),
            ),
            1_010,
        )
        .unwrap();
        let large = "x".repeat(100 * 1024);
        for index in 0_u64..11 {
            let event_at = 1_020 + index;
            execute(
                &mut database,
                &request(
                    "turn.event.record",
                    11,
                    json!({
                        "attempt_id":attempt_id,
                        "task_id":"related-task",
                        "user_id":11,
                        "slim":true,
                        "content":format!("{large} {index}"),
                        "thinking":"",
                        "now":event_at
                    }),
                    Some(&format!("related-event-{index}")),
                ),
                event_at,
            )
            .unwrap();
        }
        let announced = execute(
            &mut database,
            &request(
                "turn.related.announce",
                11,
                json!({
                    "attempt_id":attempt_id,
                    "turn_ids":[input_turn_id],
                    "user_id":11
                }),
                Some("related-announce"),
            ),
            1_040,
        )
        .unwrap();
        assert_eq!(announced, json!({"changed":true}));
        let root = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id":"related-conversation",
                    "turn_id":root_turn_id,
                    "user_id":11
                }),
                None,
            ),
            1_041,
        )
        .unwrap();
        assert_eq!(root["projection"]["content"], format!("{large} 10"));
        assert_eq!(root["projectionRevision"], initial_projection_revision + 13);
        let events = execute(
            &mut database,
            &request(
                "turn.events.list",
                11,
                json!({
                    "attempt_id":attempt_id,
                    "user_id":11,
                    "projection_mode":"patch"
                }),
                None,
            ),
            1_042,
        )
        .unwrap();
        let related_event = events.as_array().unwrap().last().unwrap();
        assert_eq!(related_event["type"], "projection_updated");
        assert_eq!(
            related_event["payload"]["updateKind"],
            "related_turns_created"
        );
        assert_eq!(
            related_event["payload"]["turns"][0]["turnId"],
            input_turn_id
        );
        assert_eq!(
            related_event["payload"]["projectionPatch"]["baseRevision"],
            initial_projection_revision + 12
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.related.announce",
                    12,
                    json!({
                        "attempt_id":attempt_id,
                        "turn_ids":[input_turn_id],
                        "user_id":12
                    }),
                    Some("foreign-related-announce"),
                ),
                1_043,
            )
            .unwrap(),
            false
        );
    }

    #[test]
    fn turn_visible_sync_filters_ghosts_inherits_live_tools_and_replays_idempotently() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.visible.sync",
                    11,
                    json!({
                        "conversation_id":"missing-visible-conversation", "user_id":11,
                        "attempt_id":"missing-visible-attempt",
                        "root_turn_id":"missing-visible-root",
                        "messages":[
                            {"role":"user", "_isVirtualUser":true, "content":""},
                            {"role":"assistant", "content":"", "thinking":"",
                             "finishReason":"aborted", "toolRounds":[]}
                        ]
                    }),
                    Some("visible-ghost-only"),
                ),
                990,
            )
            .unwrap(),
            Value::Null
        );
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"visible-conversation", "user_id":11,
                    "command_id":"visible-pair", "input_projection":{"content":"question"},
                    "conversation_defaults":{"allowCreate":true}, "now":1_000
                }),
                Some("visible-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let root_turn_id = pair["turn"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id, "task_id":"visible-task", "user_id":11}),
                Some("visible-bind"),
            ),
            1_010,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id":attempt_id, "task_id":"visible-task", "user_id":11,
                    "projection":{"content":"live", "thinking":"", "toolRounds":[{
                        "roundNum":1, "toolCallId":"visible-tool", "toolName":"run_command",
                        "toolArgs":{"cmd":"cargo test"}, "toolContent":"passed", "status":"done"
                    }]}, "now":1_020
                }),
                Some("visible-live"),
            ),
            1_020,
        )
        .unwrap();
        let visible_request = request(
            "turn.visible.sync",
            11,
            json!({
                "conversation_id":"visible-conversation", "user_id":11,
                "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                "default_kind":"flow_node", "run_id":"visible-run",
                "messages":[
                    {"role":"user", "_isVirtualUser":true, "content":"   "},
                    {"role":"assistant", "content":"", "thinking":"", "finishReason":"aborted", "toolRounds":[]},
                    {"role":"assistant", "_isFlowPlanner":true, "_flowIteration":2,
                     "content":"planner result", "thinking":"", "model":"visible-model",
                     "provider_id":"visible-provider", "toolRounds":[{
                        "roundNum":1, "toolCallId":"visible-tool", "toolName":"run_command",
                        "status":"done"
                     }]},
                    {"role":"user", "_isFlowReview":true, "_flowIteration":2,
                     "_flowApproved":false, "content":"review this"}
                ]
            }),
            Some("visible-sync"),
        );
        let synced = execute(&mut database, &visible_request, 1_030).unwrap();
        assert_eq!(synced["visibleTurnIds"].as_array().unwrap().len(), 2);
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"visible-conversation", "user_id":11}),
                None,
            ),
            1_031,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 3);
        let root = &turns[1];
        assert_eq!(root["actor"], "planner");
        assert_eq!(root["kind"], "flow_node");
        assert_eq!(
            root["projection"]["toolRounds"][0]["toolArgs"]["cmd"],
            "cargo test"
        );
        assert_eq!(root["projection"]["segments"][0]["type"], "tool_use");
        // Sealing preserves the observed live text under its durable block
        // and records the completion text as the terminal deliverable.
        assert_eq!(root["projection"]["segments"][1]["text"], "live");
        assert_eq!(root["projection"]["segments"][1]["terminal"], Value::Null);
        assert_eq!(root["projection"]["segments"][2]["text"], "planner result");
        assert_eq!(root["projection"]["segments"][2]["terminal"], json!(true));
        assert_eq!(
            root["projection"]["routeSnapshot"]["actual_model"]["model_id"],
            "visible-model"
        );
        assert_eq!(
            root["projection"]["routeSnapshot"]["provider_id"],
            "visible-provider"
        );
        assert_eq!(turns[2]["projection"]["orchestration"]["approved"], false);
        let revision = execute(
            &mut database,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id":"visible-conversation", "user_id":11}),
                None,
            ),
            1_032,
        )
        .unwrap();
        assert_eq!(revision, 4);
        let replayed = execute(
            &mut database,
            &request(
                "turn.visible.sync",
                11,
                serde_json::from_slice(&visible_request.payload).unwrap(),
                Some("visible-sync-replay"),
            ),
            1_040,
        )
        .unwrap();
        assert_eq!(replayed, synced);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.revision",
                    11,
                    json!({"conversation_id":"visible-conversation", "user_id":11}),
                    None,
                ),
                1_041,
            )
            .unwrap(),
            4
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.visible.sync",
                    12,
                    json!({
                        "conversation_id":"visible-conversation", "user_id":12,
                        "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                        "messages":[{"role":"assistant", "content":"foreign"}]
                    }),
                    Some("visible-foreign"),
                ),
                1_050,
            )
            .unwrap(),
            Value::Null
        );
    }

    #[test]
    fn turn_visible_sync_admits_live_phases_with_offsets() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"live-visible-conversation", "user_id":11,
                    "command_id":"live-visible-pair", "input_projection":{"content":"goal"},
                    "conversation_defaults":{"allowCreate":true}, "now":1_000
                }),
                Some("live-visible-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let root_turn_id = pair["turn"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id, "task_id":"live-visible-task", "user_id":11}),
                Some("live-visible-bind"),
            ),
            1_010,
        )
        .unwrap();
        // Live worker admission at the root (start_index 0): flips the root
        // to a flow node without sealing it, and creates no child rows.
        let live_worker = execute(
            &mut database,
            &request(
                "turn.visible.sync",
                11,
                json!({
                    "conversation_id":"live-visible-conversation", "user_id":11,
                    "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                    "live":true, "start_index":0,
                    "messages":[{"role":"assistant", "content":"", "thinking":"",
                                 "_flowLivePhase":true, "_flowIteration":1}]
                }),
                Some("live-visible-worker"),
            ),
            1_020,
        )
        .unwrap();
        assert_eq!(
            live_worker["visibleTurnIds"].as_array().unwrap(),
            &[json!(root_turn_id)]
        );
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"live-visible-conversation", "user_id":11}),
                None,
            ),
            1_021,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 2);
        assert_eq!(turns[1]["kind"], "flow_node");
        // Incident regression (conv mucq7uwwhww6o0, 2026-09-22): the live
        // virtual-user shell must survive admission. The ghost filter ate it
        // (no `_flowLivePhase` exemption), visible_sync returned Null, and
        // the flow aborted with a bogus "no longer owns its conversation
        // attempt" error.
        let vu_shell = json!({
            "conversation_id":"live-visible-conversation", "user_id":11,
            "attempt_id":attempt_id, "root_turn_id":root_turn_id,
            "live":true, "start_index":1,
            "messages":[{"role":"user", "_isVirtualUser":true, "_flowLivePhase":true,
                         "_flowIteration":1, "_autopilotRunId":"run-1", "content":""}]
        });
        let live_vu = execute(
            &mut database,
            &request(
                "turn.visible.sync",
                11,
                vu_shell.clone(),
                Some("live-visible-vu"),
            ),
            1_030,
        )
        .unwrap();
        let vu_turn_id = uuid::Uuid::new_v5(
            &uuid::Uuid::NAMESPACE_URL,
            format!("turn-attempt:{attempt_id}:visible:1").as_bytes(),
        )
        .to_string();
        let vu_attempt_id = uuid::Uuid::new_v5(
            &uuid::Uuid::NAMESPACE_URL,
            format!("turn-attempt:{attempt_id}:visible-attempt:1").as_bytes(),
        )
        .to_string();
        assert_eq!(
            live_vu["visibleTurnIds"].as_array().unwrap(),
            &[json!(root_turn_id), json!(vu_turn_id)]
        );
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"live-visible-conversation", "user_id":11}),
                None,
            ),
            1_031,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 3);
        let vu = &turns[2];
        assert_eq!(vu["turnId"], json!(vu_turn_id));
        assert_eq!(vu["actor"], "virtual_user");
        assert_eq!(vu["kind"], "autopilot_virtual_user");
        assert_eq!(vu["status"], "running");
        assert_eq!(vu["parentTurnId"], json!(root_turn_id));
        assert_eq!(vu["currentAttemptId"], json!(vu_attempt_id));
        assert_eq!(
            vu["projection"]["orchestration"]["iteration"],
            json!(1)
        );
        // The live phase attempt must accept its own checkpoints through the
        // flow-root fence (running status + immutable flowRootAttemptId
        // config proving the declared root).
        let checkpoint = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id":vu_attempt_id, "user_id":11,
                    "task_id":"live-visible-task",
                    "flow_root_attempt_id":attempt_id,
                    "projection":{"content":"continue", "thinking":"", "toolRounds":[]},
                    "now":1_040
                }),
                Some("live-visible-vu-checkpoint"),
            ),
            1_040,
        )
        .unwrap();
        assert_eq!(checkpoint["applied"], true);
        // Idempotent live replay re-admits the in-flight phase without error.
        let replayed_vu = execute(
            &mut database,
            &request(
                "turn.visible.sync",
                11,
                vu_shell.clone(),
                Some("live-visible-vu-replay"),
            ),
            1_050,
        )
        .unwrap();
        assert_eq!(
            replayed_vu["visibleTurnIds"].as_array().unwrap(),
            &[json!(root_turn_id), json!(vu_turn_id)]
        );
        // Once the phase settles, a live replay can never reopen it.
        let settled = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id":vu_attempt_id, "user_id":11,
                    "task_id":"live-visible-task",
                    "flow_root_attempt_id":attempt_id,
                    "terminal":true, "status":"completed",
                    "projection":{"content":"continue", "thinking":"", "toolRounds":[]},
                    "settlement":{"outcome":"completed","cause":"phase_completed",
                                  "resumeOptions":[]},
                    "now":1_060
                }),
                Some("live-visible-vu-settle"),
            ),
            1_060,
        )
        .unwrap();
        assert_eq!(settled["applied"], true);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.visible.sync",
                    11,
                    vu_shell.clone(),
                    Some("live-visible-vu-reopen"),
                ),
                1_070,
            ),
            Err(SemanticExecutionError::Conflict)
        );
        // Window validation (SQL sidecar parity): a live sync admits exactly
        // one phase, and the absolute phase window is bounded.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.visible.sync",
                    11,
                    json!({
                        "conversation_id":"live-visible-conversation", "user_id":11,
                        "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                        "live":true, "start_index":2,
                        "messages":[{"role":"assistant","content":"a"},
                                    {"role":"assistant","content":"b"}]
                    }),
                    Some("live-visible-two"),
                ),
                1_080,
            ),
            Err(SemanticExecutionError::InvalidPayload)
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.visible.sync",
                    11,
                    json!({
                        "conversation_id":"live-visible-conversation", "user_id":11,
                        "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                        "live":true, "start_index":4096,
                        "messages":[{"role":"assistant","content":"a"}]
                    }),
                    Some("live-visible-overflow"),
                ),
                1_090,
            ),
            Err(SemanticExecutionError::InvalidPayload)
        );
        // A settled root attempt owns no further visible phases.
        let settled_root = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id":attempt_id, "user_id":11,
                    "task_id":"live-visible-task",
                    "terminal":true, "status":"failed",
                    "projection":{"content":"", "thinking":"", "toolRounds":[]},
                    "settlement":{"outcome":"failed","cause":"generation_error",
                                  "resumeOptions":[]},
                    "now":1_100
                }),
                Some("live-visible-root-settle"),
            ),
            1_100,
        )
        .unwrap();
        assert_eq!(settled_root["applied"], true);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.visible.sync",
                    11,
                    json!({
                        "conversation_id":"live-visible-conversation", "user_id":11,
                        "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                        "live":true, "start_index":2,
                        "messages":[{"role":"assistant","content":"","thinking":"",
                                     "_flowLivePhase":true}]
                    }),
                    Some("live-visible-after-settle"),
                ),
                1_110,
            )
            .unwrap(),
            Value::Null
        );
    }

    #[test]
    fn turn_visible_sync_completed_import_keeps_live_marked_shells() {
        // `_partition_visible_messages` parity: completed imports drop empty
        // virtual-user ghosts, but never a shell carrying its live-phase
        // marker (it is a real admitted phase, not a projection ghost).
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"live-marked-conversation", "user_id":11,
                    "command_id":"live-marked-pair", "input_projection":{"content":"goal"},
                    "conversation_defaults":{"allowCreate":true}, "now":1_000
                }),
                Some("live-marked-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let root_turn_id = pair["turn"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id, "task_id":"live-marked-task", "user_id":11}),
                Some("live-marked-bind"),
            ),
            1_010,
        )
        .unwrap();
        let synced = execute(
            &mut database,
            &request(
                "turn.visible.sync",
                11,
                json!({
                    "conversation_id":"live-marked-conversation", "user_id":11,
                    "attempt_id":attempt_id, "root_turn_id":root_turn_id,
                    "start_index":0,
                    "messages":[
                        {"role":"assistant", "_isFlowPlanner":true, "_flowIteration":1,
                         "content":"plan", "thinking":"", "toolRounds":[]},
                        {"role":"user", "_isVirtualUser":true, "_flowLivePhase":true,
                         "_flowIteration":1, "content":""},
                        {"role":"user", "_isVirtualUser":true, "content":"  "}
                    ]
                }),
                Some("live-marked-sync"),
            ),
            1_020,
        )
        .unwrap();
        // The planner (root) and the live-marked shell survive; the bare
        // ghost is filtered.
        let shell_turn_id = uuid::Uuid::new_v5(
            &uuid::Uuid::NAMESPACE_URL,
            format!("turn-attempt:{attempt_id}:visible:1").as_bytes(),
        )
        .to_string();
        assert_eq!(
            synced["visibleTurnIds"].as_array().unwrap(),
            &[json!(root_turn_id), json!(shell_turn_id)]
        );
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"live-marked-conversation", "user_id":11}),
                None,
            ),
            1_030,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 3);
        assert_eq!(turns[2]["actor"], "virtual_user");
        assert_eq!(turns[2]["parentTurnId"], json!(root_turn_id));
    }


    #[test]
    fn artifact_domain_matches_versions_receipts_owner_scope_and_unicode_casefold() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let base = json!({
            "conv_id": "artifact-contract", "task_id": "task-a",
            "msg_id": "message-a", "source": "write_file",
            "format": "markdown", "title": "report.md",
            "source_ref": {"path": "report.md"}, "meta": {"words": 2},
        });
        let mut first_payload = base.as_object().unwrap().clone();
        first_payload.extend(
            json!({
                "artifact_id": "artifact-v1", "content": "# first\n", "created_at": 100
            })
            .as_object()
            .unwrap()
            .clone(),
        );
        let first_request = request(
            "artifact.create",
            11,
            Value::Object(first_payload),
            Some("artifact-create-v1"),
        );
        let first = execute(&mut database, &first_request, 1_000).unwrap();
        assert_eq!(first["artifact"]["version"], 1);
        assert_eq!(
            execute(&mut database, &first_request, 1_001).unwrap(),
            first
        );

        let mut second_payload = base.as_object().unwrap().clone();
        second_payload.extend(
            json!({
                "artifact_id": "artifact-v2", "content": "# second\n", "created_at": 102
            })
            .as_object()
            .unwrap()
            .clone(),
        );
        let second = execute(
            &mut database,
            &request(
                "artifact.create",
                11,
                Value::Object(second_payload),
                Some("artifact-create-v2"),
            ),
            1_002,
        )
        .unwrap();
        assert_eq!(second["artifact"]["version"], 2);
        assert_eq!(second["artifact"]["parent_id"], "artifact-v1");
        let versions = execute(
            &mut database,
            &request(
                "artifact.versions",
                11,
                json!({"artifact_id":"artifact-v2"}),
                None,
            ),
            1_003,
        )
        .unwrap();
        assert_eq!(versions[0]["id"], "artifact-v1");
        assert_eq!(versions[1]["id"], "artifact-v2");
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "artifact.pin",
                    11,
                    json!({"artifact_id":"artifact-v1","pinned":true}),
                    Some("artifact-pin-v1")
                ),
                1_004
            )
            .unwrap(),
            json!({"changed": true})
        );
        let library = execute(
            &mut database,
            &request("artifact.library", 11, json!({"limit":20}), None),
            1_005,
        )
        .unwrap();
        assert_eq!(library[0]["id"], "artifact-v1");
        let foreign = execute(
            &mut database,
            &request(
                "artifact.get",
                12,
                json!({"artifact_id":"artifact-v1","include_content":true}),
                None,
            ),
            1_006,
        )
        .unwrap();
        assert!(foreign.is_null());

        let source_prefix = format!("Straße{}", "x".repeat(500));
        let searchable = format!("{source_prefix}STRASSE İ");
        let put = execute(
            &mut database,
            &request(
                "tool_result_artifact.put",
                11,
                json!({
                    "user_id":11, "content":searchable, "media_type":"text/plain",
                    "created_at_ms":2_000, "expires_at_ms":3_000
                }),
                Some("tool-result-unicode"),
            ),
            2_000,
        )
        .unwrap();
        let artifact_ref = put["artifactRef"].as_str().unwrap();
        let search = execute(
            &mut database,
            &request(
                "tool_result_artifact.search",
                11,
                json!({
                    "user_id":11, "artifact_ref":artifact_ref, "query":"STRASSE",
                    "now_ms":2_500, "cursor":0, "limit":8
                }),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(search["items"][0]["offset"], 0);
        // Search positions address the original Unicode text, not the
        // case-folded expansion (where sharp-s becomes two characters).
        assert_eq!(search["items"][1]["offset"], source_prefix.chars().count());
        assert_eq!(
            crate::generated_unicode_casefold::PYTHON_UNICODE_CASEFOLD_VERSION,
            (15, 0, 0)
        );
        assert_eq!(
            crate::generated_unicode_casefold::PYTHON_UNICODE_CASEFOLD_MAPPING_COUNT,
            1530
        );
    }

    #[test]
    fn tool_result_semantics_admit_exact_sixteen_mib_without_outbox_duplication() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[73; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let content = "z".repeat(crate::generated_tofudb_ir::MAX_TOOL_RESULT_ARTIFACT_BYTES);
        let put_request = request(
            "tool_result_artifact.put",
            11,
            json!({
                "user_id":11, "content":content, "media_type":"text/plain",
                "created_at_ms":1_000, "expires_at_ms":2_000
            }),
            Some("tool-result-exact-sixteen-mib"),
        );
        let put = execute(&mut database, &put_request, 1_000).unwrap();
        assert_eq!(
            put["sizeBytes"],
            crate::generated_tofudb_ir::MAX_TOOL_RESULT_ARTIFACT_BYTES
        );
        let pending = database.logical_outbox_pending(7, 11, 1).unwrap();
        assert_eq!(pending.len(), 1);
        assert!(pending[0].record_bytes < 1024);
        let tail = execute(
            &mut database,
            &request(
                "tool_result_artifact.read",
                11,
                json!({
                    "user_id":11,
                    "artifact_ref":put["artifactRef"],
                    "now_ms":1_500,
                    "offset":crate::generated_tofudb_ir::MAX_TOOL_RESULT_ARTIFACT_BYTES - 7,
                    "limit":64
                }),
                None,
            ),
            1_001,
        )
        .unwrap();
        assert_eq!(tail["content"], "zzzzzzz");
        assert_eq!(tail["truncated"], false);
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[73; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(execute(&mut reopened, &put_request, 1_002).unwrap(), put);
    }

    #[test]
    fn desktop_domain_matches_get_initialize_set_and_receipt_replay_semantics() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[71; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let get = request(
            "desktop.egress_agent.get",
            11,
            json!({"owner_user_id": 11}),
            None,
        );
        assert_eq!(
            execute(&mut database, &get, 1_000).unwrap(),
            missing_preference()
        );

        let initialize = request(
            "desktop.egress_agent.initialize",
            11,
            json!({"owner_user_id": 11, "agent_id": "agent-1"}),
            Some("desktop-initialize-1"),
        );
        let initialized = execute(&mut database, &initialize, 2_000).unwrap();
        assert_eq!(initialized, present_preference("agent-1", 2_000));
        assert_eq!(
            execute(&mut database, &initialize, 9_000).unwrap(),
            initialized
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            1
        );

        let losing_initialize = request(
            "desktop.egress_agent.initialize",
            11,
            json!({"owner_user_id": 11, "agent_id": "agent-late"}),
            Some("desktop-initialize-2"),
        );
        assert_eq!(
            execute(&mut database, &losing_initialize, 3_000).unwrap(),
            initialized
        );
        let set = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "agent-2"}),
            Some("desktop-set-1"),
        );
        assert_eq!(
            execute(&mut database, &set, 4_000).unwrap(),
            present_preference("agent-2", 4_000)
        );
        assert_eq!(
            execute(&mut database, &get, 5_000).unwrap(),
            present_preference("agent-2", 4_000)
        );
        let status = database.logical_outbox_status(7, 11).unwrap();
        assert_eq!(status.last_sequence, 3);
        assert_eq!(status.published_sequence, 0);
    }

    #[test]
    fn provider_domain_is_owner_scoped_ordered_blob_capable_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 1024 * 1024)
            .unwrap();
        let create = |id: &str, owner: u64, command: &str| {
            request(
                "provider.create",
                owner,
                json!({
                    "owner_user_id": owner,
                    "tenant_id": "workspace-a",
                    "provider_id": id,
                    "name": id,
                    "base_url": "https://provider.invalid/v1",
                    "api_key_ciphertext": "x".repeat(10_000),
                    "key_hint": "…tail",
                    "models": [{"model_id": "model-a"}],
                    "extra_headers": {"X-Trace": "enabled"},
                    "thinking_format": "none",
                    "created_at": 1.25
                }),
                Some(command),
            )
        };
        let first = create("provider-a", 11, "provider-create-a");
        let created = execute(&mut database, &first, 1_250).unwrap();
        assert_eq!(
            created["api_key_ciphertext"].as_str().unwrap().len(),
            10_000
        );
        assert_eq!(execute(&mut database, &first, 9_000).unwrap(), created);
        execute(
            &mut database,
            &create("provider-b", 11, "provider-create-b"),
            1_251,
        )
        .unwrap();

        let list = request(
            "provider.list",
            11,
            json!({"owner_user_id": 11, "tenant_id": "workspace-a"}),
            None,
        );
        let listed = execute(&mut database, &list, 2_000).unwrap();
        assert_eq!(listed[0]["id"], "provider-b");
        assert_eq!(listed[1]["id"], "provider-a");
        assert!(listed[0].get("api_key_ciphertext").is_none());

        let update = request(
            "provider.update",
            11,
            json!({
                "owner_user_id": 11,
                "tenant_id": "workspace-a",
                "provider_id": "provider-a",
                "updates": {"name": "renamed", "disabled": true},
                "updated_at": 2.5
            }),
            Some("provider-update-a"),
        );
        assert_eq!(
            execute(&mut database, &update, 2_500).unwrap()["name"],
            "renamed"
        );
        let touch = request(
            "provider.touch",
            11,
            json!({
                "owner_user_id": 11,
                "tenant_id": "workspace-a",
                "provider_id": "provider-a",
                "used_at": 3.5
            }),
            Some("provider-touch-a"),
        );
        assert_eq!(
            execute(&mut database, &touch, 3_500).unwrap(),
            json!({"touched": true})
        );
        let get = request(
            "provider.get",
            11,
            json!({
                "owner_user_id": 11,
                "tenant_id": "workspace-a",
                "provider_id": "provider-a"
            }),
            None,
        );
        let touched = execute(&mut database, &get, 4_000).unwrap();
        assert_eq!(touched["updated_at"], 2.5);
        assert_eq!(touched["last_used_at"], 3.5);

        let internal_update = request(
            "provider.update",
            11,
            json!({
                "owner_user_id": 11,
                "tenant_id": "workspace-a",
                "provider_id": "provider-a",
                "updates": {"name": "valid"},
                "updated_at": 4.1
            }),
            Some("provider-internal-update"),
        );
        let admitted = admit_request(
            &internal_update,
            NegotiatedProtocol {
                protocol_version: PROTOCOL_VERSION,
                schema_id: STORAGE_SCHEMA_VERSION,
            },
            AuthenticatedScope {
                owner_id: internal_update.owner_id,
                tenant_id: internal_update.tenant_id,
            },
            4_001,
        )
        .unwrap();
        let mut ir = compile_provider(admitted, 4_001).unwrap();
        let TransactionIrStep::ProviderUpdate { updates_json, .. } = &mut ir.steps[0] else {
            panic!("provider update must compile to its dedicated IR step");
        };
        *updates_json = br#"{"owner_user_id":12}"#.to_vec();
        assert_eq!(
            crate::transaction_ir::execute_transaction_ir(&mut database, &ir)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );

        assert_eq!(
            execute(
                &mut database,
                &create("provider-a", 12, "provider-create-collision"),
                4_100,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
        let delete = request(
            "provider.delete",
            11,
            json!({
                "owner_user_id": 11,
                "tenant_id": "workspace-a",
                "provider_id": "provider-a"
            }),
            Some("provider-delete-a"),
        );
        assert_eq!(
            execute(&mut database, &delete, 5_000).unwrap(),
            json!({"deleted": true})
        );
        assert_eq!(execute(&mut database, &get, 5_001).unwrap(), Value::Null);

        for index in 0..32 {
            execute(
                &mut database,
                &create(
                    &format!("quota-provider-{index:02}"),
                    13,
                    &format!("provider-quota-create-{index:02}"),
                ),
                6_000 + index,
            )
            .unwrap();
        }
        execute(
            &mut database,
            &create("quota-provider-overflow", 13, "provider-quota-overflow"),
            7_000,
        ).unwrap();
        let quota_delete = request(
            "provider.delete",
            13,
            json!({
                "owner_user_id": 13,
                "tenant_id": "workspace-a",
                "provider_id": "quota-provider-00"
            }),
            Some("provider-quota-delete"),
        );
        assert_eq!(
            execute(&mut database, &quota_delete, 7_001).unwrap(),
            json!({"deleted": true})
        );
        execute(
            &mut database,
            &create("quota-provider-reused", 13, "provider-quota-reused"),
            7_002,
        )
        .unwrap();
    }

    #[test]
    fn model_routing_preserves_cas_migration_backup_secret_indexes_and_owner_scope() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[79; 32], 8 * 1024 * 1024, 1024 * 1024)
            .unwrap();
        let boundary = |operation: &str, owner: u64, extra: Value, command: Option<&str>| {
            let mut payload = json!({
                "owner_user_id": owner,
                "tenant_id": "workspace-a"
            });
            payload
                .as_object_mut()
                .unwrap()
                .extend(extra.as_object().unwrap().clone());
            request(operation, owner, payload, command)
        };
        let empty_document = json!({
            "contract_version": "tofu.model-routing/v2",
            "revision": 0,
            "connections": [],
            "credentials": [],
            "models": [],
            "providers": [],
            "routes": []
        });
        assert_eq!(
            execute(
                &mut database,
                &boundary("model_routing.get", 11, json!({}), None),
                1_000,
            )
            .unwrap(),
            Value::Null
        );

        let rejected_receipt = boundary(
            "model_routing.migration_receipt.put",
            11,
            json!({
                "migration_receipt": {"status": "rejected", "reason": "invalid legacy"},
                "document": empty_document,
                "updated_at": 1.0
            }),
            Some("routing-rejected-receipt"),
        );
        assert_eq!(
            execute(&mut database, &rejected_receipt, 1_000).unwrap()["revision"],
            0
        );
        let receipt_get = boundary("model_routing.migration_receipt", 11, json!({}), None);
        let rejected = execute(&mut database, &receipt_get, 1_001).unwrap();
        assert_eq!(rejected["receipt"]["status"], "rejected");
        assert!(rejected["backup"].is_null());

        let committed_document = json!({
            "contract_version": "tofu.model-routing/v2",
            "revision": 1,
            "connections": [],
            "credentials": [],
            "models": [],
            "providers": [],
            "routes": [],
            "diagnostic_padding": "r".repeat(20_000)
        });
        let commit = boundary(
            "model_routing.commit",
            11,
            json!({
                "expected_revision": 0,
                "document": committed_document,
                "migration_receipt": {"status": "committed"},
                "updated_at": 2.0
            }),
            Some("routing-commit-1"),
        );
        let acknowledgement = execute(&mut database, &commit, 2_000).unwrap();
        assert_eq!(acknowledgement["revision"], 1);
        assert!(acknowledgement.get("document").is_none());
        assert_eq!(
            execute(&mut database, &commit, 9_000).unwrap(),
            acknowledgement
        );
        let authority = execute(
            &mut database,
            &boundary("model_routing.get", 11, json!({}), None),
            2_001,
        )
        .unwrap();
        assert_eq!(authority["revision"], 1);
        assert_eq!(
            authority["document"]["diagnostic_padding"]
                .as_str()
                .unwrap()
                .len(),
            20_000
        );
        let committed_receipt = execute(&mut database, &receipt_get, 2_002).unwrap();
        assert_eq!(committed_receipt["receipt"]["status"], "committed");
        assert_eq!(committed_receipt["backup"]["revision"], 0);
        let stale = boundary(
            "model_routing.commit",
            11,
            json!({
                "expected_revision": 0,
                "document": {"contract_version": "tofu.model-routing/v2", "revision": 1},
                "updated_at": 3.0
            }),
            Some("routing-stale"),
        );
        assert_eq!(
            execute(&mut database, &stale, 3_000).unwrap_err(),
            SemanticExecutionError::Conflict
        );

        let secret_put =
            |reference: &str, ciphertext: &str, hint: &str, updated_at: f64, command: &str| {
                boundary(
                    "model_routing.secret.put",
                    11,
                    json!({
                        "secret_reference": reference,
                        "ciphertext": ciphertext,
                        "key_hint": hint,
                        "updated_at": updated_at
                    }),
                    Some(command),
                )
            };
        execute(
            &mut database,
            &secret_put("secret-b", &"b".repeat(20_000), "…bbb", 10.0, "secret-b"),
            10_000,
        )
        .unwrap();
        execute(
            &mut database,
            &secret_put("secret-a", "sealed-a", "…aaa", 20.0, "secret-a"),
            20_000,
        )
        .unwrap();
        execute(
            &mut database,
            &secret_put("secret-b", "sealed-b-new", "…new", 30.0, "secret-b-update"),
            30_000,
        )
        .unwrap();
        let listed = execute(
            &mut database,
            &boundary("model_routing.secret.list", 11, json!({}), None),
            30_001,
        )
        .unwrap();
        assert_eq!(listed[0]["secret_reference"], "secret-a");
        assert_eq!(listed[1]["secret_reference"], "secret-b");
        assert!(listed[0].get("ciphertext").is_none());
        assert_eq!(listed[1]["created_at"], 10.0);
        assert_eq!(listed[1]["updated_at"], 30.0);
        let secret = execute(
            &mut database,
            &boundary(
                "model_routing.secret.get",
                11,
                json!({"secret_reference": "secret-b"}),
                None,
            ),
            30_002,
        )
        .unwrap();
        assert_eq!(secret["ciphertext"], "sealed-b-new");
        assert!(secret.get("owner_user_id").is_none());
        assert!(secret.get("tenant_id").is_none());

        let pruned = execute(
            &mut database,
            &boundary(
                "model_routing.secret.prune",
                11,
                json!({
                    "active_secret_references": ["secret-b"],
                    "updated_before": 40.0
                }),
                Some("secret-prune"),
            ),
            40_000,
        )
        .unwrap();
        assert_eq!(pruned["removed"], json!(["secret-a"]));
        assert_eq!(pruned["count"], 1);
        assert_eq!(pruned["limit"], 256);
        assert_eq!(
            execute(
                &mut database,
                &boundary("model_routing.secret.list", 12, json!({}), None),
                40_001,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &boundary("model_routing.get", 12, json!({}), None),
                40_002,
            )
            .unwrap(),
            Value::Null
        );
        let deleted = execute(
            &mut database,
            &boundary(
                "model_routing.secret.delete",
                11,
                json!({"secret_reference": "secret-b"}),
                Some("secret-b-delete"),
            ),
            40_003,
        )
        .unwrap();
        assert_eq!(
            deleted,
            json!({"deleted": true, "secret_reference": "secret-b"})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            7
        );

        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let authority = execute(
            &mut reopened,
            &boundary("model_routing.get", 11, json!({}), None),
            50_000,
        )
        .unwrap();
        assert_eq!(authority["revision"], 1);
        assert_eq!(
            authority["document"]["diagnostic_padding"]
                .as_str()
                .unwrap()
                .len(),
            20_000
        );
        let backup = tempfile::tempdir().unwrap();
        let restored_directory = tempfile::tempdir().unwrap();
        // The raw-engine backup checkpoints without merging; drain the
        // overlay first so the backup covers every committed mutation
        // (same contract as the production CLI backup).
        reopened.merge_entity_overlay_fully().unwrap();
        crate::backup::create_incremental_backup(reopened.engine_mut_for_test(), backup.path())
            .unwrap();
        drop(reopened);
        crate::backup::restore_latest_backup(backup.path(), restored_directory.path()).unwrap();
        let mut restored = AuthorityDatabase::open(restored_directory.path()).unwrap();
        let restored_authority = execute(
            &mut restored,
            &boundary("model_routing.get", 11, json!({}), None),
            50_001,
        )
        .unwrap();
        assert_eq!(restored_authority["revision"], 1);
        assert_eq!(
            restored_authority["document"]["diagnostic_padding"]
                .as_str()
                .unwrap()
                .len(),
            20_000
        );
        let restored_receipt = execute(&mut restored, &receipt_get, 50_002).unwrap();
        assert_eq!(restored_receipt["receipt"]["status"], "committed");
        assert_eq!(restored_receipt["backup"]["revision"], 0);
    }

    #[test]
    fn worker_jobs_preserve_idempotency_priority_leases_fencing_and_cancellation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[73; 32], 8 * 1024 * 1024, 1024 * 1024)
            .unwrap();
        let enqueue = |task_id: &str,
                       owner: u64,
                       key: &str,
                       value: &str,
                       priority: u64,
                       available_at_ms: u64,
                       command: &str| {
            request(
                "worker_job.enqueue",
                owner,
                json!({
                    "task_id": task_id,
                    "user_id": owner,
                    "tenant_id": "tenant-a",
                    "task_kind": "conversation-attempt",
                    "idempotency_key": key,
                    "payload": {"value": value},
                    "priority": priority,
                    "available_at_ms": available_at_ms,
                    "now_ms": 1_000
                }),
                Some(command),
            )
        };
        let claim = |owner: u64, worker: &str, now_ms: u64, command: &str| {
            request(
                "worker_job.claim_next",
                owner,
                json!({
                    "worker_id": worker,
                    "now_ms": now_ms,
                    "lease_ms": 60_000,
                    "task_kinds": ["conversation-attempt"]
                }),
                Some(command),
            )
        };

        let large_value = "x".repeat(20_000);
        let first = enqueue(
            "worker-large",
            41,
            "stable-key",
            &large_value,
            100,
            1_000,
            "worker-enqueue-large",
        );
        let created = execute(&mut database, &first, 1_000).unwrap();
        assert_eq!(created["created"], true);
        assert_eq!(created["job"]["payload"]["value"], large_value);
        assert_eq!(execute(&mut database, &first, 1_001).unwrap(), created);

        let semantic_replay = enqueue(
            "ignored-task-id",
            41,
            "stable-key",
            &large_value,
            999,
            9_000,
            "worker-enqueue-semantic-replay",
        );
        let replayed = execute(&mut database, &semantic_replay, 1_002).unwrap();
        assert_eq!(replayed["created"], false);
        assert_eq!(replayed["job"]["taskId"], "worker-large");
        assert_eq!(
            execute(
                &mut database,
                &enqueue(
                    "changed-task-id",
                    41,
                    "stable-key",
                    "changed",
                    100,
                    1_000,
                    "worker-enqueue-conflict",
                ),
                1_003,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );

        let cross_owner_get = request(
            "worker_job.get",
            42,
            json!({"task_id": "worker-large", "user_id": 42}),
            None,
        );
        assert_eq!(
            execute(&mut database, &cross_owner_get, 1_004).unwrap(),
            Value::Null
        );
        execute(
            &mut database,
            &enqueue(
                "worker-cancel-queued",
                43,
                "cancel-key",
                "cancel",
                0,
                1_000,
                "worker-enqueue-cancel",
            ),
            1_004,
        )
        .unwrap();
        let queued_cancel = request(
            "worker_job.request_cancel",
            43,
            json!({
                "task_id": "worker-cancel-queued",
                "user_id": 43,
                "now_ms": 1_005,
                "reason": "cancel before claim"
            }),
            Some("worker-cancel-queued"),
        );
        assert_eq!(
            execute(&mut database, &queued_cancel, 1_005).unwrap()["job"]["status"],
            "cancelled"
        );
        execute(
            &mut database,
            &enqueue(
                "worker-high-priority",
                42,
                "high-key",
                "high",
                10,
                2_000,
                "worker-enqueue-high",
            ),
            1_005,
        )
        .unwrap();

        database.set_worker_lease_test_time(1500);
        let first_claim = execute(
            &mut database,
            &claim(90, "replica-a/worker", 1_500, "worker-claim-first"),
            1_500,
        )
        .unwrap();
        assert_eq!(first_claim["taskId"], "worker-large");
        assert_eq!(first_claim["fencingToken"], 1);
        database.set_worker_lease_test_time(2000);
        let second_claim = execute(
            &mut database,
            &claim(90, "replica-b/worker", 2_000, "worker-claim-second"),
            2_000,
        )
        .unwrap();
        assert_eq!(second_claim["taskId"], "worker-high-priority");
        let complete_second = request(
            "worker_job.complete",
            90,
            json!({
                "task_id": "worker-high-priority",
                "worker_id": "replica-b/worker",
                "fencing_token": 1,
                "now_ms": 2_001,
                "terminal_status": "failed",
                "error": {"detail": "e".repeat(20_000)}
            }),
            Some("worker-complete-second"),
        );
        assert_eq!(
            execute(&mut database, &complete_second, 2_001).unwrap()["job"]["status"],
            "failed"
        );

        database.set_worker_lease_test_time(61500);
        let reclaimed = execute(
            &mut database,
            &claim(91, "replica-c/worker", 61_500, "worker-claim-reclaim"),
            61_500,
        )
        .unwrap();
        assert_eq!(reclaimed["taskId"], "worker-large");
        assert_eq!(reclaimed["fencingToken"], 2);
        assert_eq!(reclaimed["attempt"], 2);

        let stale_heartbeat = request(
            "worker_job.heartbeat",
            90,
            json!({
                "task_id": "worker-large",
                "worker_id": "replica-a/worker",
                "fencing_token": 1,
                "now_ms": 61_501,
                "lease_ms": 60_000,
                "replay_cursor": 99
            }),
            Some("worker-stale-heartbeat"),
        );
        assert_eq!(
            execute(&mut database, &stale_heartbeat, 61_501).unwrap(),
            json!({"ok": false, "error": "stale_fence"})
        );
        let heartbeat = request(
            "worker_job.heartbeat",
            91,
            json!({
                "task_id": "worker-large",
                "worker_id": "replica-c/worker",
                "fencing_token": 2,
                "now_ms": 61_502,
                "lease_ms": 60_000,
                "replay_cursor": 12
            }),
            Some("worker-heartbeat"),
        );
        database.set_worker_lease_test_time(61502);
        let heartbeat_result = execute(&mut database, &heartbeat, 61_502).unwrap();
        assert_eq!(heartbeat_result["ok"], true);
        assert_eq!(heartbeat_result["job"]["replayCursor"], 12);
        let lower_cursor = request(
            "worker_job.heartbeat",
            91,
            json!({
                "task_id": "worker-large",
                "worker_id": "replica-c/worker",
                "fencing_token": 2,
                "now_ms": 61_503,
                "lease_ms": 60_000,
                "replay_cursor": 4
            }),
            Some("worker-heartbeat-lower-cursor"),
        );
        assert_eq!(
            execute(&mut database, &lower_cursor, 61_503).unwrap()["job"]["replayCursor"],
            12
        );

        let cancel = request(
            "worker_job.request_cancel",
            41,
            json!({
                "task_id": "worker-large",
                "user_id": 41,
                "now_ms": 61_504,
                "reason": "owner changed direction"
            }),
            Some("worker-cancel-running"),
        );
        let cancelled_request = execute(&mut database, &cancel, 61_504).unwrap();
        assert_eq!(cancelled_request["accepted"], true);
        assert_eq!(cancelled_request["job"]["status"], "running");
        let state = request(
            "worker_job.claim_state",
            91,
            json!({
                "task_id": "worker-large",
                "worker_id": "replica-c/worker",
                "fencing_token": 2,
                "now_ms": 61_505
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &state, 61_505).unwrap()["cancelSequence"],
            1
        );

        let complete = |status: &str, command: &str| {
            request(
                "worker_job.complete",
                91,
                json!({
                    "task_id": "worker-large",
                    "worker_id": "replica-c/worker",
                    "fencing_token": 2,
                    "now_ms": 61_506,
                    "terminal_status": status,
                    "result_ref": "record:result",
                    "replay_cursor": 19,
                    "error": {}
                }),
                Some(command),
            )
        };
        assert_eq!(
            execute(
                &mut database,
                &complete("succeeded", "worker-refused-success"),
                61_506,
            )
            .unwrap(),
            json!({"ok": false, "error": "stale_fence_or_cancelled"})
        );
        let completed = execute(
            &mut database,
            &complete("cancelled", "worker-complete-cancelled"),
            61_506,
        )
        .unwrap();
        assert_eq!(completed["job"]["status"], "cancelled");
        assert_eq!(completed["job"]["replayCursor"], 19);

        let missing_kinds = request(
            "worker_job.claim_next",
            90,
            json!({"worker_id": "unsafe-worker", "now_ms": 61_507}),
            Some("worker-missing-kinds"),
        );
        assert_eq!(
            execute(&mut database, &missing_kinds, 61_507).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
        let future_clock = request(
            "worker_job.enqueue",
            41,
            json!({
                "task_id": "worker-future-clock",
                "user_id": 41,
                "task_kind": "conversation-attempt",
                "idempotency_key": "future-clock-key",
                "payload": {},
                "now_ms": 86_461_508_u64
            }),
            Some("worker-future-clock"),
        );
        assert_eq!(
            execute(&mut database, &future_clock, 61_507).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
        let oversized_payload = request(
            "worker_job.enqueue",
            41,
            json!({
                "task_id": "worker-oversized",
                "user_id": 41,
                "task_kind": "conversation-attempt",
                "idempotency_key": "oversized-key",
                "payload": {"blob": "x".repeat(MAX_WORKER_JOB_PAYLOAD_BYTES)},
                "now_ms": 61_507
            }),
            Some("worker-oversized"),
        );
        assert_eq!(
            execute(&mut database, &oversized_payload, 61_507).unwrap_err(),
            SemanticExecutionError::PayloadTooLarge
        );
        assert_eq!(
            SemanticExecutionError::PayloadTooLarge.code(),
            "storage_payload_too_large"
        );
        assert!(!SemanticExecutionError::PayloadTooLarge.retryable());

        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[73; 32], 8 * 1024 * 1024, 1024 * 1024)
            .unwrap();
        let owned_get = request(
            "worker_job.get",
            41,
            json!({"task_id": "worker-large", "user_id": 41}),
            None,
        );
        let durable = execute(&mut reopened, &owned_get, 61_507).unwrap();
        assert_eq!(durable["payload"]["value"], large_value);
        assert_eq!(durable["status"], "cancelled");

        let backup = tempfile::tempdir().unwrap();
        let restored_directory = tempfile::tempdir().unwrap();
        // The raw-engine backup checkpoints without merging; drain the
        // overlay first so the backup covers every committed mutation
        // (same contract as the production CLI backup).
        reopened.merge_entity_overlay_fully().unwrap();
        crate::backup::create_incremental_backup(reopened.engine_mut_for_test(), backup.path())
            .unwrap();
        drop(reopened);
        crate::backup::restore_latest_backup(backup.path(), restored_directory.path()).unwrap();
        let mut restored = AuthorityDatabase::open(restored_directory.path()).unwrap();
        let restored_job = execute(&mut restored, &owned_get, 61_508).unwrap();
        assert_eq!(restored_job["payload"]["value"], large_value);
        assert_eq!(restored_job["status"], "cancelled");
    }

    fn browser_observation() -> Value {
        json!({
            "schema_version": 1,
            "strategy": "captured_api",
            "api_hints": [{
                "method": "POST",
                "origin": "https://api.internal.example",
                "path_template": "/people/{segment}/reports",
                "shape_summary": {"$": "object", "$.data": "array(2)"},
                "score": 0.91,
                "passive_only": true
            }],
            "anti_bot_vendor": "cloudflare",
            "auth_signal": "none",
            "elapsed_ms": 321,
            "capture_hint_used": false,
            "capture_hint_matched": false
        })
    }

    fn browser_identity(owner_user_id: u64) -> Value {
        json!({
            "owner_user_id": owner_user_id,
            "origin": "https://internal.example",
            "route_family": "/teams/{segment}/reports",
            "operation": "research"
        })
    }

    #[test]
    fn browser_observations_preserve_owner_expiry_and_confidence_semantics() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[92; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let mut record_payload = browser_identity(11);
        record_payload["outcome"] = Value::String("success".to_owned());
        record_payload["observed_at_ms"] = Value::from(10_000);
        record_payload["observation"] = browser_observation();
        let record = request(
            "browser.site_observation.record",
            11,
            record_payload,
            Some("browser-observation-success"),
        );
        let first = execute(&mut database, &record, 1_000).unwrap();
        assert_eq!(first["confidence_milli"], 500);
        assert_eq!(first["visit_count"], 1);
        assert_eq!(first["successful_visits"], 1);
        assert_eq!(first["api_hints"][0]["passive_only"], true);
        let replayed = execute(&mut database, &record, 9_000).unwrap();
        assert_eq!(replayed["confidence_milli"], 600);
        assert_eq!(replayed["visit_count"], 2);
        assert_eq!(replayed["successful_visits"], 2);

        let mut foreign_get = browser_identity(12);
        foreign_get["now_ms"] = Value::from(10_001);
        assert_eq!(
            execute(
                &mut database,
                &request("browser.site_observation.get", 12, foreign_get, None),
                2_000,
            )
            .unwrap(),
            Value::Null
        );
        let mut get_payload = browser_identity(11);
        get_payload["now_ms"] = Value::from(10_001);
        let get = request("browser.site_observation.get", 11, get_payload, None);
        assert_eq!(execute(&mut database, &get, 2_000).unwrap(), replayed);

        for (offset, command) in [(1, "drift-1"), (2, "drift-2"), (3, "drift-3")] {
            let mut payload = browser_identity(11);
            payload["outcome"] = Value::String("structure_mismatch".to_owned());
            payload["observed_at_ms"] = Value::from(10_000 + offset);
            let drifted = execute(
                &mut database,
                &request(
                    "browser.site_observation.record",
                    11,
                    payload,
                    Some(command),
                ),
                3_000 + offset,
            )
            .unwrap();
            if offset == 3 {
                assert_eq!(drifted["status"], "quarantined");
                assert_eq!(drifted["confidence_milli"], 0);
                let mut expired_get = browser_identity(11);
                expired_get["now_ms"] = drifted["expires_at_ms"].clone();
                assert_eq!(
                    execute(
                        &mut database,
                        &request("browser.site_observation.get", 11, expired_get, None,),
                        7_000,
                    )
                    .unwrap(),
                    Value::Null
                );
            }
        }
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            5
        );
    }

    #[test]
    fn browser_observation_rejects_active_or_sensitive_hints() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        for (command, mutate) in [
            ("active-hint", 0_u8),
            ("sensitive-hint", 1_u8),
            ("query-hint", 2_u8),
        ] {
            let mut observation = browser_observation();
            match mutate {
                0 => observation["api_hints"][0]["passive_only"] = Value::Bool(false),
                1 => {
                    observation["api_hints"][0]["shape_summary"] =
                        json!({"$.accessToken": "string"})
                }
                _ => {
                    observation["api_hints"][0]["path_template"] =
                        Value::String("/people?token=secret".to_owned())
                }
            }
            let mut payload = browser_identity(11);
            payload["outcome"] = Value::String("success".to_owned());
            payload["observed_at_ms"] = Value::from(10_000);
            payload["observation"] = observation;
            assert_eq!(
                execute(
                    &mut database,
                    &request(
                        "browser.site_observation.record",
                        11,
                        payload,
                        Some(command),
                    ),
                    1_000,
                )
                .unwrap_err(),
                SemanticExecutionError::InvalidPayload
            );
        }
    }

    #[test]
    fn browser_observation_owner_lru_is_hard_bounded() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
            .unwrap();
        for index in 0..=crate::generated_tofudb_ir::MAX_BROWSER_SITE_OBSERVATIONS_PER_OWNER {
            let operation = format!("research{index}");
            let payload = json!({
                "owner_user_id": 11,
                "origin": "https://internal.example",
                "route_family": "/teams/{segment}/reports",
                "operation": operation,
                "outcome": "success",
                "observed_at_ms": 20_000 + index as u64,
                "observation": browser_observation()
            });
            execute(
                &mut database,
                &request(
                    "browser.site_observation.record",
                    11,
                    payload,
                    Some(&format!("browser-lru-{index}")),
                ),
                1_000 + index as u64,
            )
            .unwrap();
        }
        for (operation, expected) in [
            ("research0", Value::Null),
            ("research200", Value::Bool(true)),
        ] {
            let payload = json!({
                "owner_user_id": 11,
                "origin": "https://internal.example",
                "route_family": "/teams/{segment}/reports",
                "operation": operation,
                "now_ms": 21_000
            });
            let found = execute(
                &mut database,
                &request("browser.site_observation.get", 11, payload, None),
                2_000,
            )
            .unwrap();
            if expected.is_null() {
                assert!(found.is_null());
            } else {
                assert_eq!(found["operation"], operation);
            }
        }
    }

    fn assert_browser_observation_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert!(matches!(replayed["visit_count"].as_u64(), Some(1 | 2)));
        let mut get_payload = browser_identity(11);
        get_payload["now_ms"] = Value::from(10_001);
        assert_eq!(
            execute(
                &mut recovered,
                &request("browser.site_observation.get", 11, get_payload, None),
                3_001,
            )
            .unwrap(),
            replayed
        );
        let mut transaction = recovered.begin(7, 11).unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            crate::generated_tofudb_ir::BROWSER_SITE_OBSERVATION_LRU_NAMESPACE,
            b"",
        )
        .unwrap();
        assert_eq!(
            recovered
                .entity_scan(&mut transaction, &start, &end, 2)
                .unwrap()
                .len(),
            1
        );
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        assert!(matches!(outbox_sequence, 1 | 2));
    }

    #[test]
    fn every_browser_observation_commit_fault_recovers_document_lru_and_outbox_atomically() {
        let mut payload = browser_identity(11);
        payload["outcome"] = Value::String("success".to_owned());
        payload["observed_at_ms"] = Value::from(10_000);
        payload["observation"] = browser_observation();
        let command = request(
            "browser.site_observation.record",
            11,
            payload,
            Some("faulted-browser-observation"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_browser_observation_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_browser_observation_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn recent_projects_are_owner_scoped_bounded_blob_capable_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[91; 32], 1024 * 1024, 128 * 1024)
            .unwrap();

        let touch_a = request(
            "project.recent.touch",
            11,
            json!({"user_id": 11, "project_path": "/a", "last_used": 10}),
            Some("recent-touch-a"),
        );
        let first = execute(&mut database, &touch_a, 1_000).unwrap();
        assert_eq!(first, json!({"path": "/a", "count": 1, "last_used": 10}));
        assert_eq!(execute(&mut database, &touch_a, 9_000).unwrap(), first);

        let touch_many = request(
            "project.recent.touch_many",
            11,
            json!({
                "user_id": 11,
                "project_paths": ["/b", "/a", "/b"],
                "last_used": 20
            }),
            Some("recent-touch-many"),
        );
        assert_eq!(
            execute(&mut database, &touch_many, 2_000).unwrap(),
            json!({"touched": 2})
        );

        let long_path = "界".repeat(MAX_RECENT_PROJECT_PATH_CHARACTERS);
        let touch_long = request(
            "project.recent.touch",
            11,
            json!({"user_id": 11, "project_path": long_path, "last_used": 30}),
            Some("recent-touch-long"),
        );
        assert_eq!(
            execute(&mut database, &touch_long, 3_000).unwrap()["count"],
            1
        );

        let list = request("project.recent.list", 11, json!({"user_id": 11}), None);
        let listed = execute(&mut database, &list, 4_000).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 3);
        assert_eq!(listed[0]["path"], long_path);
        assert_eq!(
            listed[1],
            json!({"path": "/a", "count": 2, "last_used": 20})
        );
        assert_eq!(
            listed[2],
            json!({"path": "/b", "count": 1, "last_used": 20})
        );
        assert_eq!(
            execute(
                &mut database,
                &request("project.recent.list", 12, json!({"user_id": 12}), None,),
                4_000,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &request("project.recent.list", 11, json!({"user_id": 12}), None,),
                4_000,
            )
            .unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            3
        );
        drop(database);

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[91; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut reopened, &list, 5_000)
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            3
        );
        let clear = request(
            "project.recent.clear",
            11,
            json!({"user_id": 11}),
            Some("recent-clear"),
        );
        assert_eq!(
            execute(&mut reopened, &clear, 6_000).unwrap(),
            json!({"deleted": 3})
        );
        assert_eq!(
            execute(&mut reopened, &clear, 7_000).unwrap(),
            json!({"deleted": 3})
        );
        assert_eq!(execute(&mut reopened, &list, 8_000).unwrap(), json!([]));
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            4
        );
    }

    #[test]
    fn daily_cost_cache_is_ordered_owner_scoped_durable_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();

        for (date, cost) in [("2026-08-13", 2.5), ("2026-08-12", 1.25)] {
            let command = request(
                "daily_cost.upsert",
                11,
                json!({
                    "user_id": 11,
                    "date": date,
                    "cost": cost,
                    "conversations": {"conv-1": {"cost": cost, "tokens": 10}},
                    "computed_at": 1_000
                }),
                Some(&format!("daily-cost-{date}")),
            );
            assert_eq!(
                execute(&mut database, &command, 2_000).unwrap(),
                json!({"saved": true})
            );
            assert_eq!(
                execute(&mut database, &command, 9_000).unwrap(),
                json!({"saved": true})
            );
        }

        let month = request(
            "daily_cost.month",
            11,
            json!({"user_id": 11, "year": 2026, "month": 8}),
            None,
        );
        let rows = execute(&mut database, &month, 3_000).unwrap();
        assert_eq!(rows[0]["date"], "2026-08-12");
        assert_eq!(rows[1]["date"], "2026-08-13");
        assert_eq!(rows[1]["conversations"]["conv-1"]["cost"], 2.5);
        assert_eq!(
            execute(
                &mut database,
                &request("daily_cost.latest", 11, json!({"user_id": 11}), None),
                3_000,
            )
            .unwrap()["date"],
            "2026-08-13"
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "daily_cost.persisted_dates",
                    11,
                    json!({
                        "user_id": 11,
                        "dates": ["2026-08-11", "2026-08-13", "2026-08-13"]
                    }),
                    None,
                ),
                3_000,
            )
            .unwrap(),
            json!({"dates": ["2026-08-13"]})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "daily_cost.month",
                    12,
                    json!({"user_id": 12, "year": 2026, "month": 8}),
                    None
                ),
                3_000,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &request("daily_cost.latest", 11, json!({"user_id": 12}), None),
                3_000,
            )
            .unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );
        drop(database);

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(execute(&mut reopened, &month, 4_000).unwrap(), rows);
        let delete_one = request(
            "daily_cost.delete",
            11,
            json!({"user_id": 11, "date": "2026-08-12"}),
            Some("daily-cost-delete-one"),
        );
        assert_eq!(
            execute(&mut reopened, &delete_one, 5_000).unwrap(),
            json!({"deleted": 1})
        );
        let delete_all = request(
            "daily_cost.delete",
            11,
            json!({"user_id": 11}),
            Some("daily-cost-delete-all"),
        );
        assert_eq!(
            execute(&mut reopened, &delete_all, 6_000).unwrap(),
            json!({"deleted": 1})
        );
        assert_eq!(
            execute(&mut reopened, &delete_all, 7_000).unwrap(),
            json!({"deleted": 1})
        );
        assert_eq!(
            execute(
                &mut reopened,
                &request("daily_cost.latest", 11, json!({"user_id": 11}), None),
                8_000,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            4
        );
    }

    fn optimizer_proposal_create(command_id: &str) -> Request {
        request(
            "optimizer.proposal.create",
            11,
            json!({
                "user_id": 11, "proposal_id": "proposal-a",
                "created_at": "2026-08-14T10:00:00",
                "title": "Bound writer queue", "rationale": "protect memory",
                "action_type": "set_limit", "action_args": "{\"limit\":200}",
                "severity": "high", "confidence": 0.9,
                "evidence": "[\"metric\"]", "status": "pending_review",
                "status_reason": ""
            }),
            Some(command_id),
        )
    }

    fn optimizer_action_record(command_id: &str) -> Request {
        request(
            "optimizer.action.record",
            11,
            json!({
                "user_id": 11, "log_id": "action-a", "proposal_id": "proposal-a",
                "applied_at": "2026-08-14T10:01:00",
                "expires_at": "2026-08-15T10:01:00", "pre_metric": "{}"
            }),
            Some(command_id),
        )
    }

    #[test]
    fn optimizer_domain_is_ordered_owner_scoped_durable_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[94; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let create = optimizer_proposal_create("optimizer-create");
        assert_eq!(
            execute(&mut database, &create, 1_000).unwrap(),
            json!({"proposal_id": "proposal-a"})
        );
        assert_eq!(
            execute(&mut database, &create, 9_000).unwrap()["proposal_id"],
            "proposal-a"
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "optimizer.proposal.get",
                    12,
                    json!({"user_id": 12, "proposal_id": "proposal-a"}),
                    None,
                ),
                1_001,
            )
            .unwrap(),
            Value::Null
        );
        execute(
            &mut database,
            &request(
                "optimizer.proposal.update",
                11,
                json!({"user_id": 11, "proposal_id": "proposal-a", "status": "applied", "reason": "verified"}),
                Some("optimizer-update"),
            ),
            2_000,
        )
        .unwrap();
        let action = optimizer_action_record("optimizer-action");
        assert_eq!(
            execute(&mut database, &action, 3_000).unwrap(),
            json!({"log_id": "action-a"})
        );
        execute(
            &mut database,
            &request(
                "optimizer.action.outcome",
                11,
                json!({"user_id": 11, "log_id": "action-a", "outcome_metric": "{\"ok\":true}", "recorded_at": "2026-08-14T11:00:00"}),
                Some("optimizer-outcome"),
            ),
            4_000,
        )
        .unwrap();
        let expired = execute(
            &mut database,
            &request(
                "optimizer.action.expired",
                11,
                json!({"user_id": 11, "now_iso": "2026-08-16T00:00:00"}),
                None,
            ),
            5_000,
        )
        .unwrap();
        assert_eq!(expired[0]["id"], "action-a");
        assert_eq!(expired[0]["p_status"], "applied");
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "optimizer.action.for_proposal",
                    11,
                    json!({"user_id": 11, "proposal_id": "proposal-a"}),
                    None,
                ),
                5_001,
            )
            .unwrap()["outcome_metric"],
            "{\"ok\":true}"
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "optimizer.proposal.list",
                    11,
                    json!({"user_id": 11, "status": "applied", "limit": 10}),
                    None,
                ),
                5_002,
            )
            .unwrap()
            .as_array()
            .unwrap()
            .len(),
            1
        );
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[94; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let include = request(
            "optimizer.action.list",
            11,
            json!({"user_id": 11, "include_reverted": true, "limit": 10}),
            None,
        );
        assert_eq!(
            execute(&mut reopened, &include, 6_000).unwrap()[0]["p_title"],
            "Bound writer queue"
        );
        execute(
            &mut reopened,
            &request(
                "optimizer.action.revert",
                11,
                json!({"user_id": 11, "log_id": "action-a", "reverted_at": "2026-08-16T00:00:01", "reason": "expired"}),
                Some("optimizer-revert"),
            ),
            7_000,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut reopened,
                &request(
                    "optimizer.action.list",
                    11,
                    json!({"user_id": 11, "include_reverted": false, "limit": 10}),
                    None,
                ),
                8_000,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(&mut reopened, &include, 8_001)
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            5
        );
    }

    fn seeded_optimizer_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &optimizer_proposal_create("optimizer-fault-seed"),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_optimizer_proposal_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 2_000).unwrap()["proposal_id"],
            "proposal-a"
        );
        let listed = execute(
            &mut recovered,
            &request(
                "optimizer.proposal.list",
                11,
                json!({"user_id": 11, "status": "", "limit": 10}),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        for namespace in [
            crate::generated_tofudb_ir::OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE,
            crate::generated_tofudb_ir::OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE,
        ] {
            let mut transaction = recovered.begin(7, 11).unwrap();
            let (start, end) = EntityKey::prefix_range(7, 11, namespace, b"").unwrap();
            assert_eq!(
                recovered
                    .entity_scan(&mut transaction, &start, &end, 2)
                    .unwrap()
                    .len(),
                1
            );
        }
        let mut transaction = recovered.begin(7, 11).unwrap();
        let status_key = EntityKey::new(
            7,
            11,
            crate::generated_tofudb_ir::OPTIMIZER_PROPOSAL_STATUS_NAMESPACE,
            b"proposal-a",
        )
        .unwrap();
        assert_eq!(
            recovered
                .entity_get(&mut transaction, &status_key)
                .unwrap()
                .as_deref(),
            Some(b"pending_review".as_slice())
        );
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    #[test]
    fn every_optimizer_proposal_create_fault_recovers_document_index_receipt_and_outbox_atomically()
    {
        let command = optimizer_proposal_create("optimizer-proposal-fault");
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 1_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_000);
            drop(database);
            assert_optimizer_proposal_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_000);
            drop(database);
            assert_optimizer_proposal_fault_replay_converges(vfs, &command);
        }
    }

    fn assert_optimizer_action_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap()["log_id"],
            "action-a"
        );
        let listed = execute(
            &mut recovered,
            &request(
                "optimizer.action.list",
                11,
                json!({"user_id": 11, "include_reverted": true, "limit": 10}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["id"], "action-a");
        for namespace in [
            crate::generated_tofudb_ir::OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE,
            crate::generated_tofudb_ir::OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE,
            crate::generated_tofudb_ir::OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE,
            crate::generated_tofudb_ir::OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE,
        ] {
            let mut transaction = recovered.begin(7, 11).unwrap();
            let (start, end) = EntityKey::prefix_range(7, 11, namespace, b"").unwrap();
            assert_eq!(
                recovered
                    .entity_scan(&mut transaction, &start, &end, 2)
                    .unwrap()
                    .len(),
                1
            );
        }
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2 | 3
        ));
    }

    #[test]
    fn every_optimizer_action_record_fault_recovers_document_indexes_receipt_and_outbox_atomically()
    {
        let command = optimizer_action_record("optimizer-action-fault");
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_optimizer_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_optimizer_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_optimizer_action_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_optimizer_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_optimizer_action_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn log_aggregates_are_owner_scoped_durable_and_preserve_merge_semantics() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let flush = request(
            "log_aggregate.flush",
            11,
            json!({"rows": [{
                "fingerprint": "aaa.disk-full", "level": "error",
                "logger": "alpha", "template": "disk full",
                "sample": "s1", "count": 2,
                "first_seen": 1000, "last_seen": 5000
            }]}),
            Some("la-flush-1"),
        );
        assert_eq!(
            execute(&mut database, &flush, 2_000).unwrap(),
            json!({"flushed": 1, "swept": 0})
        );
        let query = request("log_aggregate.query", 11, json!({}), None);
        assert_eq!(
            execute(&mut database, &query, 3_000).unwrap(),
            json!({
                "items": [{
                    "fingerprint": "aaa.disk-full", "level": "error",
                    "logger": "alpha", "template": "disk full",
                    "sample": "s1", "count": 2,
                    "first_seen": 1000, "last_seen": 5000
                }],
                "total_rows": 1,
                "total_events": 2
            })
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "log_aggregate.flush",
                    11,
                    json!({"rows": [{
                        "fingerprint": "aaa.disk-full", "level": "fatal",
                        "logger": "changed", "template": "changed",
                        "sample": "s2", "count": 3,
                        "first_seen": 500, "last_seen": 6000
                    }]}),
                    Some("la-flush-2"),
                ),
                4_000,
            )
            .unwrap(),
            json!({"flushed": 1, "swept": 0})
        );
        let merged = execute(&mut database, &query, 5_000).unwrap();
        assert_eq!(merged["items"][0]["level"], "error");
        assert_eq!(merged["items"][0]["logger"], "alpha");
        assert_eq!(merged["items"][0]["template"], "disk full");
        assert_eq!(merged["items"][0]["sample"], "s2");
        assert_eq!(merged["items"][0]["count"], 5);
        assert_eq!(merged["items"][0]["first_seen"], 1000);
        assert_eq!(merged["items"][0]["last_seen"], 6000);
        assert_eq!(merged["total_events"], 5);
        assert_eq!(
            execute(
                &mut database,
                &request("log_aggregate.query", 12, json!({}), None),
                5_001,
            )
            .unwrap(),
            json!({"items": [], "total_rows": 0, "total_events": 0})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );
        drop(database);

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(execute(&mut reopened, &query, 6_000).unwrap(), merged);
    }

    fn log_aggregate_fault_command() -> Request {
        request(
            "log_aggregate.flush",
            11,
            json!({"rows": [{
                "fingerprint": "fault.aggregate", "level": "error",
                "logger": "fault", "template": "fault aggregate",
                "sample": "sample", "count": 2,
                "first_seen": 1000, "last_seen": 2000
            }]}),
            Some("log-aggregate-fault"),
        )
    }

    fn assert_log_aggregate_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replay["flushed"], 1);
        assert_eq!(replay["swept"], 0);
        let result = execute(
            &mut recovered,
            &request("log_aggregate.query", 11, json!({}), None),
            3_001,
        )
        .unwrap();
        assert_eq!(result["items"].as_array().unwrap().len(), 1);
        assert_eq!(result["items"][0]["fingerprint"], "fault.aggregate");
        assert!(matches!(result["items"][0]["count"].as_u64(), Some(2 | 4)));
        assert_eq!(result["total_rows"], 1);
        assert!(matches!(result["total_events"].as_u64(), Some(2 | 4)));
        let mut transaction = recovered.begin(7, 11).unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            crate::generated_tofudb_ir::LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE,
            b"",
        )
        .unwrap();
        assert_eq!(
            recovered
                .entity_scan(&mut transaction, &start, &end, 2)
                .unwrap()
                .len(),
            1
        );
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    #[test]
    fn every_log_aggregate_flush_fault_recovers_document_index_and_outbox_atomically() {
        let command = log_aggregate_fault_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_log_aggregate_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_log_aggregate_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn plugin_manifests_are_owner_scoped_durable_and_enforce_append_only_versions() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let manifest_v1 = json!({
            "namespace": "alpha.beta", "version": 1,
            "tables": [{
                "name": "items",
                "columns": [
                    {"name": "id", "type": "string"},
                    {"name": "note", "type": "string", "required": true},
                ],
                "primary_key": ["id"],
                "indexes": [{"name": "by_note", "columns": ["note"]}],
            }],
            "operations": [
                {"name": "fetch", "action": "get", "table": "items"},
                {"name": "store", "action": "put", "table": "items", "limit_max": 50},
            ],
        });
        let register_v1 = request(
            "plugin.register",
            11,
            json!({"manifest": manifest_v1.clone()}),
            Some("pg-reg-1"),
        );
        assert_eq!(
            execute(&mut database, &register_v1, 2_000).unwrap(),
            json!({"namespace": "alpha.beta", "version": 1})
        );
        let get = request(
            "plugin.manifest.get",
            11,
            json!({"namespace": "alpha.beta"}),
            None,
        );
        let stored = execute(&mut database, &get, 3_000).unwrap();
        assert_eq!(stored["namespace"], "alpha.beta");
        assert_eq!(stored["version"], 1);
        assert_eq!(stored["tables"][0]["columns"][0]["required"], false);
        assert_eq!(stored["tables"][0]["columns"][1]["required"], true);
        assert_eq!(stored["tables"][0]["indexes"][0]["unique"], false);
        assert_eq!(stored["operations"][0]["kind"], "query");
        assert_eq!(stored["operations"][0]["limit_max"], 100);
        assert_eq!(stored["operations"][1]["kind"], "command");
        assert_eq!(stored["operations"][1]["limit_max"], 50);
        // Identical re-register at the same version is a no-op.
        assert_eq!(
            execute(&mut database, &register_v1, 3_500).unwrap(),
            json!({"namespace": "alpha.beta", "version": 1})
        );
        // Same version with a different definition is rejected.
        let mut redefined = manifest_v1.clone();
        redefined["operations"][1]["limit_max"] = json!(51);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.register",
                    11,
                    json!({"manifest": redefined}),
                    Some("pg-reg-2"),
                ),
                4_000,
            )
            .unwrap_err(),
            SemanticExecutionError::PluginStorageIncompatible
        );
        // A malformed manifest is rejected with the same typed error.
        let mut malformed = manifest_v1.clone();
        malformed["namespace"] = json!("AB");
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.register",
                    11,
                    json!({"manifest": malformed}),
                    Some("pg-reg-3"),
                ),
                4_100,
            )
            .unwrap_err(),
            SemanticExecutionError::PluginStorageIncompatible
        );
        // A missing manifest validates as JSON null and is rejected likewise.
        assert_eq!(
            execute(
                &mut database,
                &request("plugin.register", 11, json!({}), Some("pg-reg-4")),
                4_200,
            )
            .unwrap_err(),
            SemanticExecutionError::PluginStorageIncompatible
        );
        // Append-only upgrade: new optional column, new table, new operation.
        let manifest_v2 = json!({
            "namespace": "alpha.beta", "version": 2,
            "tables": [
                {
                    "name": "items",
                    "columns": [
                        {"name": "id", "type": "string", "required": false},
                        {"name": "note", "type": "string", "required": true},
                        {"name": "extra", "type": "json"},
                    ],
                    "primary_key": ["id"],
                    "indexes": [{"name": "by_note", "columns": ["note"], "unique": false}],
                },
                {
                    "name": "logs",
                    "columns": [{"name": "id", "type": "string"}],
                    "primary_key": ["id"],
                    "indexes": [],
                },
            ],
            "operations": [
                {"name": "fetch", "action": "get", "table": "items", "limit_max": 100},
                {"name": "store", "action": "put", "table": "items", "limit_max": 50},
                {"name": "listing", "action": "list", "table": "logs"},
            ],
        });
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.register",
                    11,
                    json!({"manifest": manifest_v2.clone()}),
                    Some("pg-reg-5"),
                ),
                5_000,
            )
            .unwrap(),
            json!({"namespace": "alpha.beta", "version": 2})
        );
        assert_eq!(execute(&mut database, &get, 5_500).unwrap()["version"], 2);
        // A backwards version is rejected.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.register",
                    11,
                    json!({"manifest": manifest_v1.clone()}),
                    Some("pg-reg-6"),
                ),
                6_000,
            )
            .unwrap_err(),
            SemanticExecutionError::PluginStorageIncompatible
        );
        // A v3 that requires a new column is rejected.
        let mut required_growth = manifest_v2.clone();
        required_growth["version"] = json!(3);
        required_growth["tables"][0]["columns"][2]["required"] = json!(true);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.register",
                    11,
                    json!({"manifest": required_growth}),
                    Some("pg-reg-7"),
                ),
                6_500,
            )
            .unwrap_err(),
            SemanticExecutionError::PluginStorageIncompatible
        );
        // Manifests are owner-scoped: another owner sees a plain miss.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.manifest.get",
                    12,
                    json!({"namespace": "alpha.beta"}),
                    None,
                ),
                7_000,
            )
            .unwrap(),
            Value::Null
        );
        // An unknown namespace is a miss.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "plugin.manifest.get",
                    11,
                    json!({"namespace": "unknown.space"}),
                    None,
                ),
                7_100,
            )
            .unwrap(),
            Value::Null
        );
        // Legacy `_required_text` rejects empty, missing, and non-string
        // namespaces as database_protocol_error.
        for payload in [
            json!({"namespace": ""}),
            json!({}),
            json!({"namespace": 123}),
            json!({"namespace": "x".repeat(129)}),
        ] {
            assert_eq!(
                execute(
                    &mut database,
                    &request("plugin.manifest.get", 11, payload, None),
                    7_200,
                )
                .unwrap_err(),
                SemanticExecutionError::InvalidPayload
            );
        }
        // Only two distinct mutating commands committed: pg-reg-1 and pg-reg-5;
        // the identical re-register replayed the pg-reg-1 receipt.
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );
        drop(database);

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[93; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let persisted = execute(&mut reopened, &get, 8_000).unwrap();
        assert_eq!(persisted["version"], 2);
        assert_eq!(persisted["tables"].as_array().unwrap().len(), 2);
        assert_eq!(persisted["operations"].as_array().unwrap().len(), 3);
    }

    fn plugin_fault_command() -> Request {
        request(
            "plugin.register",
            11,
            json!({
                "manifest": {
                    "namespace": "fault.plugin", "version": 1,
                    "tables": [{
                        "name": "items",
                        "columns": [{"name": "id", "type": "string"}],
                        "primary_key": ["id"],
                        "indexes": [],
                    }],
                    "operations": [{"name": "fetch", "action": "get", "table": "items"}],
                },
            }),
            Some("plugin-fault"),
        )
    }

    fn assert_plugin_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replay["namespace"], "fault.plugin");
        assert_eq!(replay["version"], 1);
        let stored = execute(
            &mut recovered,
            &request(
                "plugin.manifest.get",
                11,
                json!({"namespace": "fault.plugin"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(stored["namespace"], "fault.plugin");
        assert_eq!(stored["version"], 1);
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    #[test]
    fn every_plugin_register_fault_recovers_manifest_and_outbox_atomically() {
        let command = plugin_fault_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_plugin_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_plugin_fault_replay_converges(vfs, &command);
        }
    }

    fn seeded_log_aggregate_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "log_aggregate.flush",
                11,
                json!({"rows": [
                    {
                        "fingerprint": "fault.aggregate", "level": "error",
                        "logger": "fault", "template": "fault aggregate",
                        "sample": "seed", "count": 1,
                        "first_seen": 1000, "last_seen": 1000
                    },
                    {
                        "fingerprint": "stale.aggregate", "level": "info",
                        "logger": "fault", "template": "stale aggregate",
                        "sample": "stale", "count": 3,
                        "first_seen": 500, "last_seen": 500
                    }
                ]}),
                Some("log-aggregate-seed"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn log_aggregate_update_and_sweep_command() -> Request {
        request(
            "log_aggregate.flush",
            11,
            json!({
                "rows": [{
                    "fingerprint": "fault.aggregate", "level": "fatal",
                    "logger": "changed", "template": "changed",
                    "sample": "updated", "count": 2,
                    "first_seen": 100, "last_seen": 2000
                }],
                "cutoff_ms": 1500
            }),
            Some("log-aggregate-update-and-sweep"),
        )
    }

    fn assert_log_aggregate_update_and_sweep_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replay["flushed"], 1);
        assert!(matches!(replay["swept"].as_u64(), Some(0 | 1)));
        let result = execute(
            &mut recovered,
            &request("log_aggregate.query", 11, json!({}), None),
            3_001,
        )
        .unwrap();
        assert_eq!(result["items"].as_array().unwrap().len(), 1);
        assert_eq!(result["items"][0]["fingerprint"], "fault.aggregate");
        assert_eq!(result["items"][0]["level"], "error");
        assert_eq!(result["items"][0]["sample"], "updated");
        assert!(matches!(result["items"][0]["count"].as_u64(), Some(3 | 5)));
        assert_eq!(result["total_rows"], 1);
        assert!(matches!(result["total_events"].as_u64(), Some(3 | 5)));
        let mut transaction = recovered.begin(7, 11).unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            crate::generated_tofudb_ir::LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE,
            b"",
        )
        .unwrap();
        assert_eq!(
            recovered
                .entity_scan(&mut transaction, &start, &end, 2)
                .unwrap()
                .len(),
            1
        );
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2 | 3
        ));
    }

    #[test]
    fn every_log_aggregate_update_and_sweep_fault_recovers_one_atomic_state() {
        let command = log_aggregate_update_and_sweep_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_log_aggregate_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_log_aggregate_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_log_aggregate_update_and_sweep_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_log_aggregate_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_log_aggregate_update_and_sweep_fault_replay_converges(vfs, &command);
        }
    }

    fn assert_daily_cost_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"saved": true})
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "daily_cost.month",
                    11,
                    json!({"user_id": 11, "year": 2026, "month": 9}),
                    None,
                ),
                3_001,
            )
            .unwrap(),
            json!([{
                "date": "2026-09-04",
                "cost": 4.5,
                "conversations": {"fault-conversation": {"cost": 4.5}},
                "computed_at": 2_000
            }])
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_daily_cost_commit_fault_recovers_document_count_receipt_and_outbox_atomically() {
        let command = request(
            "daily_cost.upsert",
            11,
            json!({
                "user_id": 11,
                "date": "2026-09-04",
                "cost": 4.5,
                "conversations": {"fault-conversation": {"cost": 4.5}},
                "computed_at": 2_000
            }),
            Some("faulted-daily-cost"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_daily_cost_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_daily_cost_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn project_brain_work_lifecycle_is_owner_scoped_durable_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[95; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let task_id = "project-brain-task-1";
        let digest = format!("{:x}", sha2::Sha256::digest(task_id.as_bytes()));
        let work_id = format!("pw_{}", &digest[..24]);
        let start = request(
            "project_brain.work.start",
            11,
            json!({
                "owner_user_id": 11,
                "project_key": "/workspace/project/",
                "work_item": {
                    "id": work_id,
                    "taskId": task_id,
                    "conversationId": "conversation-1",
                    "title": "Initial title",
                    "trigger": "file_write",
                    "status": "active",
                    "changedPaths": [],
                    "artifacts": [],
                    "resultSummary": "",
                    "startedAt": 1,
                    "finishedAt": null,
                    "_titlePriority": 100,
                    "_titleRefined": false
                },
                "timestamp": 1
            }),
            Some("project-brain-start-1"),
        );
        let started = execute(&mut database, &start, 1_000).unwrap();
        assert_eq!(started["event"]["kind"], "work_started");
        assert_eq!(started["projection"]["projectKey"], "/workspace/project");
        assert_eq!(execute(&mut database, &start, 9_000).unwrap(), started);

        let refine = request(
            "project_brain.work.refine",
            11,
            json!({
                "owner_user_id": 11, "project_key": "/workspace/project",
                "work_id": work_id, "title": "Refined title",
                "title_priority": 200, "timestamp": 2
            }),
            Some("project-brain-refine-1"),
        );
        assert_eq!(
            execute(&mut database, &refine, 2_000).unwrap()["event"]["kind"],
            "work_title_refined"
        );
        let change = request(
            "project_brain.work.change",
            11,
            json!({
                "owner_user_id": 11, "project_key": "/workspace/project",
                "work_id": work_id,
                "changed_paths": ["src/lib.rs", "src/lib.rs"],
                "artifacts": [{"id": "artifact-1", "title": "Patch", "format": "diff", "path": "change.diff"}],
                "timestamp": 3
            }),
            Some("project-brain-change-1"),
        );
        execute(&mut database, &change, 3_000).unwrap();
        let finish = request(
            "project_brain.work.finish",
            11,
            json!({
                "owner_user_id": 11, "project_key": "/workspace/project",
                "work_id": work_id, "status": "completed",
                "result_summary": "Implemented", "timestamp": 4
            }),
            Some("project-brain-finish-1"),
        );
        let finished = execute(&mut database, &finish, 4_000).unwrap();
        assert_eq!(
            finished["projection"]["workItems"][0]["status"],
            "completed"
        );
        assert_eq!(
            finished["projection"]["workItems"][0]["title"],
            "Refined title"
        );
        assert_eq!(
            finished["projection"]["workItems"][0]["changedPaths"],
            json!(["src/lib.rs"])
        );
        assert!(finished["projection"]["workItems"][0]
            .get("_titlePriority")
            .is_none());
        assert_eq!(
            finished["projection"]["narratives"][0]["kind"],
            "work_result"
        );
        let narrative = request(
            "project_brain.narrative.add",
            11,
            json!({
                "owner_user_id": 11, "project_key": "/workspace/project",
                "kind": "decision", "text": "Keep the immutable stream",
                "conversation_id": "conversation-1", "timestamp": 5
            }),
            Some("project-brain-narrative-1"),
        );
        execute(&mut database, &narrative, 5_000).unwrap();
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            5
        );
        let prepare = request(
            "project_brain.cursor.prepare",
            11,
            json!({
                "owner_user_id": 11, "project_key": "/workspace/project",
                "conversation_id": "feed-consumer", "timestamp": 6
            }),
            Some("project-brain-prepare-1"),
        );
        assert_eq!(
            execute(&mut database, &prepare, 6_000).unwrap()["initialized"],
            true
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            6
        );
        let prepared_again = execute(
            &mut database,
            &request(
                "project_brain.cursor.prepare",
                11,
                serde_json::from_slice(&prepare.payload).unwrap(),
                Some("project-brain-prepare-2"),
            ),
            6_001,
        )
        .unwrap();
        assert_eq!(prepared_again["initialized"], false);
        assert_eq!(prepared_again["entries"], json!([]));
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            6
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "project_brain.get",
                    12,
                    json!({"owner_user_id": 12, "project_key": "/workspace/project"}),
                    None,
                ),
                6_000,
            )
            .unwrap()["headSequence"],
            0
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "project_brain.get",
                    11,
                    json!({"owner_user_id": 12, "project_key": "/workspace/project"}),
                    None,
                ),
                6_000,
            )
            .unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch
        );
        drop(database);

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let projection = execute(
            &mut reopened,
            &request(
                "project_brain.get",
                11,
                json!({"owner_user_id": 11, "project_key": "/workspace/project"}),
                None,
            ),
            7_000,
        )
        .unwrap();
        assert_eq!(projection["headSequence"], 6);
        assert_eq!(
            projection["narratives"][1]["text"],
            "Keep the immutable stream"
        );
    }

    #[test]
    fn project_brain_payload_limit_maps_to_the_legacy_non_retryable_error() {
        let error = io::Error::new(
            io::ErrorKind::OutOfMemory,
            "storage_payload_too_large: Project active-work limit reached",
        );
        assert_eq!(
            map_storage_error(error),
            SemanticExecutionError::PayloadTooLarge
        );
        assert!(!SemanticExecutionError::PayloadTooLarge.retryable());
    }

    fn project_brain_fault_command() -> Request {
        let task_id = "project-brain-fault-task";
        let digest = format!("{:x}", sha2::Sha256::digest(task_id.as_bytes()));
        request(
            "project_brain.work.start",
            11,
            json!({
                "owner_user_id": 11,
                "project_key": "/fault-project",
                "work_item": {
                    "id": format!("pw_{}", &digest[..24]),
                    "taskId": task_id,
                    "conversationId": "fault-conversation",
                    "title": "Atomic Project Brain transition",
                    "trigger": "file_write",
                    "status": "active",
                    "changedPaths": [],
                    "artifacts": [],
                    "resultSummary": "",
                    "startedAt": 1,
                    "finishedAt": null,
                    "_titlePriority": 100,
                    "_titleRefined": false
                },
                "timestamp": 1
            }),
            Some("project-brain-fault-command"),
        )
    }

    fn assert_project_brain_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["event"]["kind"], "work_started");
        let projection = execute(
            &mut recovered,
            &request(
                "project_brain.get",
                11,
                json!({"owner_user_id": 11, "project_key": "/fault-project"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(projection["headSequence"], 1);
        assert_eq!(projection["workItems"].as_array().unwrap().len(), 1);
        let active = execute(
            &mut recovered,
            &request(
                "project_brain.active.list",
                11,
                json!({"owner_user_id": 11}),
                None,
            ),
            3_002,
        )
        .unwrap();
        assert_eq!(active["projects"].as_array().unwrap().len(), 1);
        assert_eq!(active["projects"][0]["projectKey"], "/fault-project");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_project_brain_commit_fault_recovers_projection_stream_receipt_and_outbox_atomically() {
        let command = project_brain_fault_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_project_brain_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_project_brain_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn project_brain_recovery_snapshot_is_a_pure_bounded_maintenance_read() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();

        let response = execute(
            &mut database,
            &request(
                "project_brain.recovery.snapshot",
                11,
                json!({}),
                Some("project-brain-recovery-snapshot"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(response, json!({"projects": [], "capped": false}));
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            0
        );
    }

    #[test]
    fn native_project_brain_authority_is_born_cutover_complete() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();

        let status = request("project_brain.cutover.status", 11, json!({}), None);
        assert_eq!(
            execute(&mut database, &status, 2_000).unwrap(),
            json!({"complete": true})
        );
        let cutover = request(
            "project_brain.cutover",
            11,
            json!({}),
            Some("project-brain-cutover-v1"),
        );
        let expected = json!({
            "ok": true,
            "alreadyComplete": true,
            "projects": 0
        });
        assert_eq!(execute(&mut database, &cutover, 2_001).unwrap(), expected);
        assert_eq!(execute(&mut database, &cutover, 2_002).unwrap(), expected);
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            1
        );
    }

    fn assert_recent_project_batch_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"touched": 2})
        );
        let rows = execute(
            &mut recovered,
            &request("project.recent.list", 11, json!({"user_id": 11}), None),
            3_001,
        )
        .unwrap();
        assert_eq!(
            rows,
            json!([
                {"path": "/fault-a", "count": 1, "last_used": 2000},
                {"path": "/fault-b", "count": 1, "last_used": 2000}
            ])
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_recent_project_batch_commit_fault_recovers_one_atomic_prefix() {
        let command = request(
            "project.recent.touch_many",
            11,
            json!({
                "user_id": 11,
                "project_paths": ["/fault-b", "/fault-a"],
                "last_used": 2_000
            }),
            Some("faulted-recent-project-batch"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_recent_project_batch_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_recent_project_batch_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn conversation_create_preserves_count_receipt_owner_and_global_id_semantics() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            database
                .configure_logical_outbox(&[73; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
                .unwrap();
            let create = request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "conversation-1",
                    "user_id": 11,
                    "title": "界".repeat(501),
                    "settings": {"large": "x".repeat(16 * 1024)},
                    "created_at": 0,
                    "updated_at": 0
                }),
                Some("conversation-create-1"),
            );
            assert_eq!(
                execute(&mut database, &create, 2_000).unwrap(),
                json!({"applied": true, "rev": 0})
            );
            assert_eq!(
                execute(&mut database, &create, 9_000).unwrap(),
                json!({"applied": true, "rev": 0})
            );
            let get = request(
                "conversation.get",
                11,
                json!({
                    "conv_id": "conversation-1",
                    "user_id": 11,
                    "message_window": 5
                }),
                None,
            );
            assert_eq!(
                execute(&mut database, &get, 9_000).unwrap(),
                json!({
                    "metadata": {
                        "id": "conversation-1",
                        "user_id": 11,
                        "title": "界".repeat(500),
                        "created_at": 0,
                        "updated_at": 0,
                        "settings": {"large": "x".repeat(16 * 1024)},
                        "msg_count": 0,
                        "search_text": "",
                        "rev": 0
                    },
                    "messages": [],
                    "source": "sidecar",
                    "message_page": {"total_count": 0, "start": 0, "end": 0}
                })
            );
            let settings_update = request(
                "conversation.settings.update",
                11,
                json!({
                    "conv_id": "conversation-1",
                    "user_id": 11,
                    "updates": {"model": "new"},
                    "expected_rev": 0
                }),
                Some("conversation-settings-1"),
            );
            assert_eq!(
                execute(&mut database, &settings_update, 9_010).unwrap(),
                json!({"applied": true, "missing": false, "conflict": false, "rev": 0})
            );
            assert_eq!(
                execute(&mut database, &settings_update, 9_011).unwrap(),
                json!({"applied": true, "missing": false, "conflict": false, "rev": 0})
            );
            let replacement_conflict = request(
                "conversation.settings.update",
                11,
                json!({
                    "conv_id": "conversation-1",
                    "user_id": 11,
                    "updates": {"only": true},
                    "replace": true,
                    "expected_settings": {"model": "stale"}
                }),
                Some("conversation-settings-conflict"),
            );
            assert_eq!(
                execute(&mut database, &replacement_conflict, 9_020).unwrap(),
                json!({"applied": false, "missing": false, "conflict": true, "rev": 0})
            );
            let metadata_update = request(
                "conversation.metadata.update",
                11,
                json!({
                    "conv_id": "conversation-1",
                    "user_id": 11,
                    "updates": {"title": "renamed", "updated_at": 0}
                }),
                Some("conversation-metadata-1"),
            );
            assert_eq!(
                execute(&mut database, &metadata_update, 9_030).unwrap(),
                json!({"applied": true, "missing": false, "rev": 0})
            );
            let updated = execute(&mut database, &get, 9_040).unwrap();
            assert_eq!(updated["metadata"]["title"], "renamed");
            assert_eq!(updated["metadata"]["updated_at"], 0);
            assert_eq!(updated["metadata"]["rev"], 0);
            assert_eq!(updated["metadata"]["settings"]["model"], "new");
            assert_eq!(
                updated["metadata"]["settings"]["large"],
                Value::String("x".repeat(16 * 1024))
            );
            let cross_owner_get = request(
                "conversation.get",
                12,
                json!({"conv_id": "conversation-1", "user_id": 12}),
                None,
            );
            assert_eq!(
                execute(&mut database, &cross_owner_get, 9_000).unwrap(),
                Value::Null
            );
            let owner_count = request("conversation.count", 11, json!({"user_id": 11}), None);
            assert_eq!(
                execute(&mut database, &owner_count, 9_001).unwrap(),
                json!({"count": 1})
            );
            let other_count = request("conversation.count", 12, json!({"user_id": 12}), None);
            assert_eq!(
                execute(&mut database, &other_count, 9_002).unwrap(),
                json!({"count": 0})
            );
            let cross_owner_duplicate = request(
                "conversation.create",
                12,
                json!({"conv_id": "conversation-1", "user_id": 12}),
                Some("conversation-create-2"),
            );
            assert_eq!(
                execute(&mut database, &cross_owner_duplicate, 9_003).unwrap_err(),
                SemanticExecutionError::Conflict
            );
            assert_eq!(
                database.logical_outbox_status(7, 11).unwrap().last_sequence,
                4
            );

            let mut scoped = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let unrelated_global_key =
                EntityKey::new(7, u64::MAX, "not_conversation_claims", b"x").unwrap();
            assert_eq!(
                database
                    .entity_get(&mut scoped, &unrelated_global_key)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::PermissionDenied
            );
            for namespace in [
                crate::generated_tofudb_ir::CONVERSATION_ID_CLAIM_NAMESPACE,
                crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
                crate::generated_tofudb_ir::ATTEMPT_ID_CLAIM_NAMESPACE,
            ] {
                let claim_key = EntityKey::new(7, u64::MAX, namespace, b"probe").unwrap();
                assert!(database
                    .entity_get(&mut scoped, &claim_key)
                    .unwrap()
                    .is_none());
            }
            let wrong_tenant_claim = EntityKey::new(
                8,
                u64::MAX,
                crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
                b"probe",
            )
            .unwrap();
            assert_eq!(
                database
                    .entity_get(&mut scoped, &wrong_tenant_claim)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::PermissionDenied
            );
            let mut ordinary = database.begin(7, 11).unwrap();
            let global_claim = EntityKey::new(
                7,
                u64::MAX,
                crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
                b"turn-human",
            )
            .unwrap();
            assert_eq!(
                database
                    .entity_get(&mut ordinary, &global_claim)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::PermissionDenied
            );
        }

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let owner_count = request("conversation.count", 11, json!({"user_id": 11}), None);
        assert_eq!(
            execute(&mut reopened, &owner_count, 10_000).unwrap(),
            json!({"count": 1})
        );
    }

    #[test]
    fn large_compaction_archive_uses_blob_budget_and_survives_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, &request("conversation.create", 11,
            json!({"conv_id":"large-archive", "user_id":11}), Some("large-create")), 1_000).unwrap();
        let content = "界".repeat(3 * 1024 * 1024);
        let create = request("compaction_archive.create", 11, json!({
            "archive_id":"large-archive-1", "conversation_id":"large-archive", "user_id":11,
            "messages":[{"role":"tool", "content":content}], "created_at_ms":2_000
        }), Some("large-archive-create"));
        assert!(create.payload.len() > crate::protocol::MAX_PAYLOAD_BYTES);
        let saved = execute(&mut database, &create, 2_000).unwrap();
        assert_eq!(execute(&mut database, &create, 2_001).unwrap(), saved);
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let get = request("compaction_archive.get", 11, json!({
            "archive_id":"large-archive-1", "conversation_id":"large-archive", "user_id":11,
            "include_messages":true
        }), None);
        let recovered = execute(&mut database, &get, 3_000).unwrap();
        assert_eq!(recovered["messages"][0]["content"], content);
        let foreign = request("compaction_archive.get", 12, json!({
            "archive_id":"large-archive-1", "conversation_id":"large-archive", "user_id":12
        }), None);
        assert_eq!(execute(&mut database, &foreign, 3_001), Err(SemanticExecutionError::NotFound));
    }

    #[test]
    fn compaction_archives_are_bounded_owner_scoped_and_clone_without_payload_rewrite() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[91; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        for (owner, conversation, command) in [
            (11, "archive-source", "archive-conversation-1"),
            (12, "archive-foreign", "archive-conversation-2"),
        ] {
            execute(
                &mut database,
                &request(
                    "conversation.create",
                    owner,
                    json!({"conv_id": conversation, "user_id": owner, "title": "Archive", "settings": {}}),
                    Some(command),
                ),
                1_000,
            )
            .unwrap();
        }
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "compaction_archive.create",
                    11,
                    json!({
                        "archive_id": "private-envelope",
                        "conversation_id": "archive-source",
                        "user_id": 11,
                        "messages": [{"_tofuArchivedMessageCodec": {}}]
                    }),
                    Some("private-envelope-create"),
                ),
                1_500,
            ),
            Err(SemanticExecutionError::InvalidPayload)
        );
        let large_content = "payload".repeat(20_000);
        let create = request(
            "compaction_archive.create",
            11,
            json!({
                "archive_id": "archive-1",
                "conversation_id": "archive-source",
                "user_id": 11,
                "messages": [{"role": "user", "content": large_content}],
                "summary": "before",
                "receipt": {},
                "trigger": "manual",
                "task_id": "historical-task",
                "round_num": 4,
                "model": "model-a",
                "tokens_before": 900,
                "tokens_after": 0,
                "msgs_before": 1,
                "msgs_after": 0,
                "reason": "budget",
                "created_at_ms": 2_000
            }),
            Some("archive-create-1"),
        );
        assert_eq!(
            execute(&mut database, &create, 2_000).unwrap(),
            json!({"created": true, "archiveId": "archive-1"})
        );
        assert_eq!(
            execute(&mut database, &create, 9_000).unwrap(),
            json!({"created": true, "archiveId": "archive-1"})
        );
        let list = request(
            "compaction_archive.list",
            11,
            json!({"conversation_id": "archive-source", "user_id": 11}),
            None,
        );
        let listed = execute(&mut database, &list, 3_000).unwrap();
        assert_eq!(listed["archives"][0]["id"], "archive-1");
        assert_eq!(listed["archives"][0]["summaryPreview"], "before");
        assert!(listed["archives"][0].get("receipt").is_none());
        let source_messages_reference_before = {
            let mut transaction = database.begin(7, 11).unwrap();
            let key = EntityKey::new(
                7,
                11,
                crate::generated_tofudb_ir::COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
                b"archive-1",
            )
            .unwrap();
            let stored = database
                .entity_get(&mut transaction, &key)
                .unwrap()
                .unwrap();
            crate::versioned_document::stored_blob_reference(&stored)
                .unwrap()
                .expect("large archive messages must use a blob")
        };

        let update = request(
            "compaction_archive.update_summary",
            11,
            json!({
                "archive_id": "archive-1",
                "user_id": 11,
                "summary": "after",
                "tokens_after": 300,
                "msgs_after": 2,
                "receipt": {"status": "completed", "strategy": "selective_summary"}
            }),
            Some("archive-update-1"),
        );
        assert_eq!(
            execute(&mut database, &update, 4_000).unwrap(),
            json!({"updated": true})
        );
        let get = request(
            "compaction_archive.get",
            11,
            json!({"conversation_id": "archive-source", "archive_id": "archive-1", "user_id": 11, "include_messages": true}),
            None,
        );
        let loaded = execute(&mut database, &get, 5_000).unwrap();
        assert_eq!(loaded["archive"]["summary"], "after");
        assert_eq!(loaded["archive"]["resultStatus"], "completed");
        assert_eq!(loaded["messages"][0]["content"], large_content);
        let source_messages_reference_after = {
            let mut transaction = database.begin(7, 11).unwrap();
            let key = EntityKey::new(
                7,
                11,
                crate::generated_tofudb_ir::COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
                b"archive-1",
            )
            .unwrap();
            let stored = database
                .entity_get(&mut transaction, &key)
                .unwrap()
                .unwrap();
            crate::versioned_document::stored_blob_reference(&stored)
                .unwrap()
                .unwrap()
        };
        assert_eq!(
            source_messages_reference_after,
            source_messages_reference_before
        );

        let collision = request(
            "compaction_archive.create",
            12,
            json!({"archive_id": "archive-1", "conversation_id": "archive-foreign", "user_id": 12, "messages": []}),
            Some("archive-collision"),
        );
        assert_eq!(
            execute(&mut database, &collision, 6_000),
            Err(SemanticExecutionError::Conflict)
        );

        let clone = request(
            "conversation.clone",
            11,
            json!({"conv_id": "archive-source", "destination_conv_id": "archive-clone", "user_id": 11}),
            Some("archive-clone-command"),
        );
        assert_eq!(
            execute(&mut database, &clone, 7_000).unwrap()["archiveCount"],
            1
        );
        let clone_list = execute(
            &mut database,
            &request(
                "compaction_archive.list",
                11,
                json!({"conversation_id": "archive-clone", "user_id": 11}),
                None,
            ),
            8_000,
        )
        .unwrap();
        let cloned_id = clone_list["archives"][0]["id"].as_str().unwrap().to_owned();
        assert_ne!(cloned_id, "archive-1");
        assert_ne!(clone_list["archives"][0]["taskId"], "historical-task");
        let cloned = execute(
            &mut database,
            &request(
                "compaction_archive.get",
                11,
                json!({"conversation_id": "archive-clone", "archive_id": cloned_id, "user_id": 11}),
                None,
            ),
            8_100,
        )
        .unwrap();
        assert_eq!(cloned["messages"], loaded["messages"]);
        assert_eq!(cloned["archive"]["summary"], "after");
        let cloned_messages_reference = {
            let mut transaction = database.begin(7, 11).unwrap();
            let key = EntityKey::new(
                7,
                11,
                crate::generated_tofudb_ir::COMPACTION_ARCHIVE_MESSAGES_NAMESPACE,
                cloned_id.as_bytes(),
            )
            .unwrap();
            let stored = database
                .entity_get(&mut transaction, &key)
                .unwrap()
                .unwrap();
            crate::versioned_document::stored_blob_reference(&stored)
                .unwrap()
                .unwrap()
        };
        assert_eq!(cloned_messages_reference, source_messages_reference_before);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.purge",
                    11,
                    json!({"conv_id": "archive-clone", "user_id": 11}),
                    Some("archive-clone-purge"),
                ),
                8_200,
            )
            .unwrap(),
            json!({"purged": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "compaction_archive.create",
                    12,
                    json!({
                        "archive_id": cloned_id,
                        "conversation_id": "archive-foreign",
                        "user_id": 12,
                        "messages": []
                    }),
                    Some("archive-reuse-after-purge"),
                ),
                8_300,
            )
            .unwrap()["created"],
            true
        );

        execute(
            &mut database,
            &request(
                "compaction_archive.create",
                11,
                json!({
                    "archive_id": "archive-2",
                    "conversation_id": "archive-source",
                    "user_id": 11,
                    "messages": [{"role": "user", "content": "newer"}],
                    "created_at_ms": 8_500
                }),
                Some("archive-create-2"),
            ),
            8_500,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "compaction_archive.prune",
                    11,
                    json!({"conversation_id": "archive-source", "user_id": 11, "keep": 1}),
                    Some("archive-prune"),
                ),
                8_600,
            )
            .unwrap(),
            json!({"deleted": 1})
        );
        assert_eq!(
            execute(&mut database, &list, 8_700).unwrap()["archives"][0]["id"],
            "archive-2"
        );
        execute(
            &mut database,
            &request(
                "conversation.delete",
                11,
                json!({"conv_id": "archive-source", "user_id": 11}),
                Some("archive-source-delete"),
            ),
            8_800,
        )
        .unwrap();
        assert_eq!(
            execute(&mut database, &list, 8_850),
            Err(SemanticExecutionError::NotFound)
        );
        execute(
            &mut database,
            &request(
                "conversation.restore",
                11,
                json!({"conv_id": "archive-source", "user_id": 11}),
                Some("archive-source-restore"),
            ),
            8_900,
        )
        .unwrap();
        assert_eq!(
            execute(&mut database, &list, 8_950).unwrap()["archives"][0]["id"],
            "archive-2"
        );

        assert_eq!(
            execute(
                &mut database,
                &request(
                    "compaction_archive.delete_conversation",
                    11,
                    json!({"conversation_id": "archive-source", "user_id": 11}),
                    Some("archive-delete-all")
                ),
                9_000,
            )
            .unwrap(),
            json!({"deleted": 1})
        );
        assert_eq!(
            execute(&mut database, &list, 9_100).unwrap(),
            json!({"archives": []})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            10
        );
    }

    #[test]
    fn conversation_clone_remaps_an_inert_snapshot_without_touching_the_source() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[83; 32], 1024 * 1024, 256 * 1024)
            .unwrap();
        let create = request(
            "conversation.create",
            11,
            json!({
                "conv_id": "clone-source",
                "user_id": 11,
                "title": "Source",
                "settings": {"folderId": "folder-a", "activeTaskId": "live-task"}
            }),
            Some("clone-source-create"),
        );
        execute(&mut database, &create, 1_000).unwrap();
        let human = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "clone-source",
                "user_id": 11,
                "actor": "human",
                "command_id": "clone-source-human-command",
                "turn_id": "clone-source-human",
                "projection": {"content": "hello"}
            }),
            Some("clone-source-human-receipt"),
        );
        execute(&mut database, &human, 1_010).unwrap();
        let assistant = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "clone-source",
                "user_id": 11,
                "actor": "assistant",
                "command_id": "clone-source-assistant-command",
                "turn_id": "clone-source-assistant",
                "projection": {
                    "content": "world",
                    "approvalRequired": {"id": "must-not-copy"},
                    "toolRounds": [{"taskId": "source-task", "status": "done"}],
                    "timingTrace": {"taskId": "source-task", "status": "done", "running": false}
                }
            }),
            Some("clone-source-assistant-receipt"),
        );
        let source_assistant = execute(&mut database, &assistant, 1_020).unwrap();
        let source_attempt_id = source_assistant["attempt"]["attemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        let clone = request(
            "conversation.clone",
            11,
            json!({
                "conv_id": "clone-source",
                "destination_conv_id": "clone-destination",
                "user_id": 11,
                "title": "Snapshot"
            }),
            Some("clone-source-to-destination"),
        );
        let cloned = execute(&mut database, &clone, 2_000).unwrap();
        assert_eq!(
            cloned,
            json!({
                "cloned": true,
                "missing": false,
                "busy": false,
                "conversationId": "clone-destination",
                "turnCount": 2,
                "archiveCount": 0,
                "rev": 0
            })
        );
        assert_eq!(execute(&mut database, &clone, 2_001).unwrap(), cloned);
        let get_destination = request(
            "conversation.get",
            11,
            json!({"conv_id": "clone-destination", "user_id": 11}),
            None,
        );
        let destination = execute(&mut database, &get_destination, 2_010).unwrap();
        assert_eq!(destination["metadata"]["title"], "Snapshot");
        assert_eq!(
            destination["metadata"]["settings"],
            json!({"folderId": "folder-a", "clonedFrom": "clone-source"})
        );
        assert_eq!(destination["metadata"]["msg_count"], 2);
        assert_eq!(destination["messages"][0]["content"], "hello");
        assert_eq!(destination["messages"][1]["content"], "world");
        assert_ne!(
            destination["messages"][0]["_turnId"],
            Value::String("clone-source-human".to_owned())
        );
        assert!(destination["messages"][0].get("_msgId").is_none());
        assert!(destination["messages"][1]["_attemptId"].is_null());
        assert!(destination["messages"][1].get("approvalRequired").is_none());
        let cloned_task = destination["messages"][1]["toolRounds"][0]["taskId"]
            .as_str()
            .unwrap();
        assert!(cloned_task.starts_with("clone-task-"));
        assert_ne!(cloned_task, "source-task");
        assert_eq!(
            destination["messages"][1]["timingTrace"]["taskId"],
            cloned_task
        );
        let snapshot = request(
            "turn.sync.snapshot",
            11,
            json!({"conversation_id": "clone-destination", "user_id": 11}),
            None,
        );
        let snapshot = execute(&mut database, &snapshot, 2_020).unwrap();
        assert_eq!(
            snapshot["turns"][1]["parentTurnId"],
            snapshot["turns"][0]["turnId"]
        );
        let source_attempt = request(
            "turn.attempt.get",
            11,
            json!({"attempt_id": source_attempt_id, "user_id": 11}),
            None,
        );
        assert!(!execute(&mut database, &source_attempt, 2_030)
            .unwrap()
            .is_null());
        let owner_count = request("conversation.count", 11, json!({"user_id": 11}), None);
        assert_eq!(
            execute(&mut database, &owner_count, 2_040).unwrap()["count"],
            2
        );
        let foreign = request(
            "conversation.clone",
            12,
            json!({
                "conv_id": "clone-source",
                "destination_conv_id": "foreign-clone",
                "user_id": 12
            }),
            Some("foreign-clone"),
        );
        assert_eq!(
            execute(&mut database, &foreign, 2_050).unwrap(),
            json!({"cloned": false, "missing": true, "busy": false})
        );
    }

    #[test]
    fn conversation_clone_terminalizes_a_live_authority_snapshot() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let create = request(
            "conversation.create",
            11,
            json!({"conv_id": "live-source", "user_id": 11, "title": "Live"}),
            Some("live-source-create"),
        );
        execute(&mut database, &create, 1_000).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut transaction,
            &crate::turn::AppendSettledRequest {
                conversation_id: "live-source".to_owned(),
                actor: "assistant".to_owned(),
                status: "running".to_owned(),
                projection_json: serde_json::to_vec(&json!({
                    "content": "visible partial answer",
                    "approvalRequired": {"id": "live-approval"},
                    "activeTaskId": "live-task",
                    "toolRounds": [
                        {"taskId": "live-task", "status": "done", "toolContent": "kept"},
                        {"taskId": "live-task", "status": "awaiting_human"}
                    ],
                    "timingTrace": {"taskId": "live-task", "status": "running", "running": true},
                    "segments": [
                        {"type": "tool_use", "result": {"status": "done"}},
                        {"type": "tool_use", "result": {"status": "running"}}
                    ]
                }))
                .unwrap(),
                projection_timestamp: None,
                settlement_json: serde_json::to_vec(&json!({
                    "outcome": "running",
                    "cause": "streaming",
                    "resumeOptions": []
                }))
                .unwrap(),
                lane_id: "main".to_owned(),
                command_id: "live-turn-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: "live-run".to_owned(),
                turn_id: "live-source-turn".to_owned(),
                attempt_id: Some("live-source-attempt".to_owned()),
                created_at_ms: 1_010,
                committed_at_ms: 1_010,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: false,
                    title: String::new(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let clone = request(
            "conversation.clone",
            11,
            json!({
                "conv_id": "live-source",
                "destination_conv_id": "live-destination",
                "user_id": 11
            }),
            Some("live-source-clone"),
        );
        assert!(execute(&mut database, &clone, 2_000).unwrap()["cloned"]
            .as_bool()
            .unwrap());
        let snapshot = request(
            "turn.sync.snapshot",
            11,
            json!({"conversation_id": "live-destination", "user_id": 11}),
            None,
        );
        let cloned = execute(&mut database, &snapshot, 2_010).unwrap();
        let turn = &cloned["turns"][0];
        assert_eq!(turn["status"], "interrupted");
        assert!(turn["currentAttemptId"].is_null());
        assert_eq!(turn["runId"], "");
        assert_eq!(turn["settlement"]["cause"], "conversation_cloned_snapshot");
        assert_eq!(turn["projection"]["content"], "visible partial answer");
        assert!(turn["projection"].get("approvalRequired").is_none());
        assert!(turn["projection"].get("activeTaskId").is_none());
        assert_eq!(turn["projection"]["toolRounds"][0]["status"], "done");
        assert_eq!(turn["projection"]["toolRounds"][1]["status"], "aborted");
        assert_eq!(turn["projection"]["toolRounds"][0]["toolContent"], "kept");
        assert_eq!(turn["projection"]["timingTrace"]["status"], "aborted");
        assert_eq!(turn["projection"]["timingTrace"]["running"], false);
        assert_eq!(
            turn["projection"]["segments"][0]["result"]["status"],
            "done"
        );
        assert_eq!(
            turn["projection"]["segments"][1]["result"]["status"],
            "aborted"
        );
        let cloned_task_id = turn["projection"]["toolRounds"][0]["taskId"]
            .as_str()
            .unwrap();
        assert!(cloned_task_id.starts_with("clone-task-"));
        assert_eq!(turn["projection"]["timingTrace"]["taskId"], cloned_task_id);
        let source = request(
            "turn.get",
            11,
            json!({
                "conversation_id": "live-source",
                "turn_id": "live-source-turn",
                "user_id": 11
            }),
            None,
        );
        let source = execute(&mut database, &source, 2_020).unwrap();
        assert_eq!(source["status"], "running");
        assert_eq!(source["currentAttemptId"], "live-source-attempt");
        assert_eq!(source["runId"], "live-run");
    }

    #[test]
    fn conversation_clone_range_witness_rejects_a_mixed_projection_snapshot() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "clone-race-source", "user_id": 11}),
                Some("clone-race-source-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "clone-race-source",
                    "user_id": 11,
                    "command_id": "clone-race-turn-command",
                    "actor": "assistant",
                    "turn_id": "clone-race-turn",
                    "projection": {"content": "before"}
                }),
                Some("clone-race-turn-append"),
            ),
            1_010,
        )
        .unwrap();
        let mut clone_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::clone_conversation(
            &database,
            &mut clone_transaction,
            &crate::conversation_header::CloneRequest {
                source_conversation_id: "clone-race-source".to_owned(),
                destination_conversation_id: "clone-race-destination".to_owned(),
                title: None,
                identity_seed: [91; 32],
                committed_at_ms: 2_000,
            },
        )
        .unwrap();
        let update = request(
            "turn.projection.update",
            11,
            json!({
                "conversation_id": "clone-race-source",
                "turn_id": "clone-race-turn",
                "user_id": 11,
                "projection": {"content": "after"},
                "expected_projection_revision": 1,
                "updated_at": 2_001
            }),
            Some("clone-race-source-update"),
        );
        execute(&mut database, &update, 2_001).unwrap();
        assert_eq!(
            database.commit(clone_transaction).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        let destination = request(
            "conversation.get",
            11,
            json!({"conv_id": "clone-race-destination", "user_id": 11}),
            None,
        );
        assert!(execute(&mut database, &destination, 2_010)
            .unwrap()
            .is_null());
        let source = request(
            "turn.get",
            11,
            json!({
                "conversation_id": "clone-race-source",
                "turn_id": "clone-race-turn",
                "user_id": 11
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &source, 2_011).unwrap()["projection"]["content"],
            "after"
        );
    }

    #[test]
    fn conversation_delete_atomically_detaches_active_and_executable_state() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[84; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "trash-conversation",
                    "user_id": 11,
                    "title": "Recoverable",
                    "settings": {"activeTaskId": "must-not-reactivate"}
                }),
                Some("trash-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "trash-conversation-child", "user_id": 11}),
                Some("trash-prefix-neighbor-create"),
            ),
            1_001,
        )
        .unwrap();
        let appended = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "trash-conversation",
                    "user_id": 11,
                    "command_id": "trash-append",
                    "actor": "assistant",
                    "projection": {"content": "x".repeat(20 * 1024)},
                    "turn_id": "trash-turn"
                }),
                Some("trash-append"),
            ),
            2_000,
        )
        .unwrap();
        let attempt_id = appended["attempt"]["attemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        let delete_request = request(
            "conversation.delete",
            11,
            json!({"conv_id": "trash-conversation", "user_id": 11}),
            Some("trash-delete"),
        );
        let deleted = json!({"deleted": true, "recoverable": true, "deletedAt": 3_000});
        assert_eq!(
            execute(&mut database, &delete_request, 3_000).unwrap(),
            deleted
        );
        assert_eq!(
            execute(&mut database, &delete_request, 9_000).unwrap(),
            deleted
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.get",
                    11,
                    json!({"conv_id": "trash-conversation", "user_id": 11}),
                    None,
                ),
                3_001,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut database,
                &request("conversation.count", 11, json!({"user_id": 11}), None),
                3_002,
            )
            .unwrap(),
            json!({"count": 1})
        );
        assert!(execute(
            &mut database,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "trash-conversation-child", "user_id": 11}),
                None,
            ),
            3_002,
        )
        .unwrap()
        .is_object());
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.list",
                    11,
                    json!({"conversation_id": "trash-conversation", "user_id": 11}),
                    None,
                ),
                3_003,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.attempt.get",
                    11,
                    json!({"attempt_id": attempt_id, "user_id": 11}),
                    None,
                ),
                3_004,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.delete",
                    11,
                    json!({"conv_id": "trash-conversation", "user_id": 11}),
                    Some("trash-delete-again"),
                ),
                3_005,
            )
            .unwrap(),
            json!({"deleted": false, "alreadyDeleted": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.restore",
                    12,
                    json!({"conv_id": "trash-conversation", "user_id": 12}),
                    Some("foreign-trash-restore"),
                ),
                3_006,
            )
            .unwrap(),
            json!({"restored": false, "conflict": false, "missing": true})
        );

        let pin_id = {
            let transaction = database.begin(7, 11).unwrap();
            crate::conversation_header::trash_pin_id(&transaction, "trash-conversation")
        };
        let mut trash = database
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .unwrap();
        let trashed_turns =
            crate::turn::list_values_until(&database, &mut trash, "trash-conversation", None, &mut || false).unwrap();
        assert_eq!(trashed_turns.len(), 1);
        assert_eq!(
            trashed_turns[0]["projection"]["content"]
                .as_str()
                .unwrap()
                .len(),
            20 * 1024
        );
        drop(trash);
        drop(database);

        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[84; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        let mut trash = database
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .unwrap();
        assert_eq!(
            crate::turn::list_values_until(&database, &mut trash, "trash-conversation", None, &mut || false)
                .unwrap()
                .len(),
            1
        );
        drop(trash);

        let restore_request = request(
            "conversation.restore",
            11,
            json!({"conv_id": "trash-conversation", "user_id": 11}),
            Some("trash-restore"),
        );
        let restored = json!({
            "restored": true,
            "conflict": false,
            "missing": false,
            "rev": 2,
            "turnCount": 1
        });
        assert_eq!(
            execute(&mut database, &restore_request, 4_000).unwrap(),
            restored
        );
        assert_eq!(
            execute(&mut database, &restore_request, 9_000).unwrap(),
            restored
        );
        assert!(database
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .is_none());
        let conversation = execute(
            &mut database,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "trash-conversation", "user_id": 11}),
                None,
            ),
            4_001,
        )
        .unwrap();
        assert_eq!(conversation["metadata"]["rev"], 2);
        assert_eq!(conversation["metadata"]["settings"], json!({}));
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id": "trash-conversation", "user_id": 11}),
                None,
            ),
            4_002,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 1);
        assert_eq!(turns[0]["currentAttemptId"], Value::Null);
        assert_eq!(turns[0]["runId"], "");
        assert!(turns[0].get("_executionEpoch").is_none());
        assert_eq!(
            turns[0]["projection"]["content"].as_str().unwrap().len(),
            20 * 1024
        );
        let snapshot = execute(
            &mut database,
            &request(
                "turn.sync.snapshot",
                11,
                json!({"conversation_id": "trash-conversation", "user_id": 11}),
                None,
            ),
            4_003,
        )
        .unwrap();
        assert_eq!(snapshot["attempts"], json!([]));
        assert_eq!(snapshot["syncSequence"], 0);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.restore",
                    11,
                    json!({"conv_id": "trash-conversation", "user_id": 11}),
                    Some("trash-restore-again"),
                ),
                4_004,
            )
            .unwrap(),
            json!({"restored": false, "conflict": true, "missing": false})
        );
        let appended_after_restore = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "trash-conversation",
                    "user_id": 11,
                    "command_id": "post-restore-append",
                    "actor": "assistant",
                    "projection": {"content": "new"},
                    "turn_id": "post-restore-turn"
                }),
                Some("post-restore-append"),
            ),
            5_000,
        )
        .unwrap();
        let new_attempt = appended_after_restore["attempt"]["attemptId"]
            .as_str()
            .unwrap();
        assert!(execute(
            &mut database,
            &request(
                "turn.attempt.get",
                11,
                json!({"attempt_id": new_attempt, "user_id": 11}),
                None,
            ),
            5_001,
        )
        .unwrap()
        .is_object());
    }

    #[test]
    fn conversation_purge_removes_trash_capsule_age_indexes_and_global_identities() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[85; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "purge-conversation", "user_id": 11}),
                Some("purge-create"),
            ),
            1_000,
        )
        .unwrap();
        let appended = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "purge-conversation",
                    "user_id": 11,
                    "command_id": "purge-append",
                    "actor": "assistant",
                    "projection": {"content": "retired"},
                    "turn_id": "purge-turn"
                }),
                Some("purge-append"),
            ),
            2_000,
        )
        .unwrap();
        let attempt_id = appended["attempt"]["attemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        execute(
            &mut database,
            &request(
                "turn.delete",
                11,
                json!({
                    "conversation_id": "purge-conversation",
                    "user_id": 11,
                    "turn_ids": ["purge-turn"]
                }),
                Some("purge-turn-delete"),
            ),
            3_000,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.delete",
                11,
                json!({"conv_id": "purge-conversation", "user_id": 11}),
                Some("purge-delete"),
            ),
            4_000,
        )
        .unwrap();
        let pin_id = {
            let transaction = database.begin(7, 11).unwrap();
            crate::conversation_header::trash_pin_id(&transaction, "purge-conversation")
        };
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.purge",
                    11,
                    json!({"conv_id": "purge-conversation", "user_id": 11}),
                    Some("purge-final")
                ),
                5_000,
            )
            .unwrap(),
            json!({"purged": true})
        );
        assert!(database
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .is_none());
        let mut claims = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for (namespace, identity) in [
            (
                crate::generated_tofudb_ir::CONVERSATION_ID_CLAIM_NAMESPACE,
                "purge-conversation",
            ),
            (
                crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
                "purge-turn",
            ),
            (
                crate::generated_tofudb_ir::ATTEMPT_ID_CLAIM_NAMESPACE,
                attempt_id.as_str(),
            ),
        ] {
            let key = EntityKey::new(7, u64::MAX, namespace, identity.as_bytes()).unwrap();
            assert!(database.entity_get(&mut claims, &key).unwrap().is_none());
        }
        drop(claims);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.purge",
                    11,
                    json!({"conv_id": "purge-conversation", "user_id": 11}),
                    Some("purge-missing")
                ),
                5_001,
            )
            .unwrap(),
            json!({"purged": false})
        );
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "purge-conversation", "user_id": 11}),
                Some("purge-recreate"),
            ),
            6_000,
        )
        .unwrap();
        assert!(execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "purge-conversation",
                    "user_id": 11,
                    "command_id": "purge-reappend",
                    "actor": "human",
                    "projection": {"content": "reused safely"},
                    "turn_id": "purge-turn"
                }),
                Some("purge-reappend"),
            ),
            7_000,
        )
        .unwrap()
        .is_object());
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.purge",
                    11,
                    json!({"conv_id": "purge-conversation", "user_id": 11}),
                    Some("purge-active")
                ),
                8_000,
            )
            .unwrap(),
            json!({"purged": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request("conversation.count", 11, json!({"user_id": 11}), None),
                8_001,
            )
            .unwrap(),
            json!({"count": 0})
        );
    }

    #[test]
    fn conversation_trash_prune_is_owner_scoped_oldest_first_and_bounded() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[86; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        for (conversation_id, deleted_at_ms) in [("prune-old", 2_000), ("prune-new", 3_000)] {
            execute(
                &mut database,
                &request(
                    "conversation.create",
                    11,
                    json!({"conv_id": conversation_id, "user_id": 11}),
                    Some(&format!("create-{conversation_id}")),
                ),
                1_000,
            )
            .unwrap();
            execute(
                &mut database,
                &request(
                    "conversation.delete",
                    11,
                    json!({"conv_id": conversation_id, "user_id": 11}),
                    Some(&format!("delete-{conversation_id}")),
                ),
                deleted_at_ms,
            )
            .unwrap();
        }
        let prune = |command_id: &str| {
            request(
                "conversation.trash.prune",
                11,
                json!({"deleted_before_ms": 4_000, "max_conversations": 1}),
                Some(command_id),
            )
        };
        assert_eq!(
            execute(&mut database, &prune("prune-page-1"), 5_000).unwrap(),
            json!({"purgedConversations": 1, "remaining": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.restore",
                    11,
                    json!({"conv_id": "prune-old", "user_id": 11}),
                    Some("restore-pruned-old")
                ),
                5_001,
            )
            .unwrap(),
            json!({"restored": false, "conflict": false, "missing": true})
        );
        assert_eq!(
            execute(&mut database, &prune("prune-page-2"), 5_002).unwrap(),
            json!({"purgedConversations": 1, "remaining": false})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "conversation.trash.prune",
                    12,
                    json!({"deleted_before_ms": 4_000, "max_conversations": 1}),
                    Some("foreign-prune")
                ),
                5_003,
            )
            .unwrap(),
            json!({"purgedConversations": 0, "remaining": false})
        );
    }

    #[test]
    fn conversation_updated_index_matches_sql_descending_order_and_rekeys() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (conversation_id, updated_at, folder_id) in [
            ("a", 100, "folder-1"),
            ("aa", 100, "folder-1"),
            ("b", 100, "folder-2"),
            ("z", 200, "folder-2"),
        ] {
            let create = request(
                "conversation.create",
                11,
                json!({
                    "conv_id": conversation_id,
                    "user_id": 11,
                    "title": conversation_id.to_uppercase(),
                    "updated_at": updated_at,
                    "settings": {"folderId": folder_id, "projectPath": "/project"}
                }),
                Some(&format!("create-index-{conversation_id}")),
            );
            execute(&mut database, &create, 1_000).unwrap();
        }
        let indexed_ids = |database: &AuthorityDatabase| {
            let mut transaction = database.begin(7, 11).unwrap();
            let (start, end) = EntityKey::prefix_range(
                7,
                11,
                crate::generated_tofudb_ir::CONVERSATION_UPDATED_INDEX_NAMESPACE,
                b"",
            )
            .unwrap();
            database
                .entity_scan(&mut transaction, &start, &end, 10)
                .unwrap()
                .into_iter()
                .map(|(_, value)| {
                    let (logical_key, document) =
                        crate::versioned_document::materialize_stored_document(
                            database,
                            7,
                            11,
                            &value,
                            "conversations",
                        )
                        .unwrap();
                    assert_eq!(
                        serde_json::from_slice::<Value>(&document).unwrap()["id"],
                        logical_key
                    );
                    logical_key
                })
                .collect::<Vec<_>>()
        };
        assert_eq!(indexed_ids(&database), ["z", "b", "aa", "a"]);

        let list_ids = |value: &Value| {
            value
                .as_array()
                .unwrap()
                .iter()
                .map(|document| document["metadata"]["id"].as_str().unwrap().to_owned())
                .collect::<Vec<_>>()
        };
        let recent = request(
            "conversation.list",
            11,
            json!({"user_id": 11, "include_messages": false, "limit": 10}),
            None,
        );
        assert_eq!(
            list_ids(&execute(&mut database, &recent, 1_500).unwrap()),
            ["z", "b", "aa", "a"]
        );
        let by_id = request(
            "conversation.list",
            11,
            json!({
                "user_id": 11,
                "order_by": "id_asc",
                "project_path": "/project",
                "title_contains": "A",
                "settings_keys": ["folderId"],
                "limit": 10
            }),
            None,
        );
        let by_id_value = execute(&mut database, &by_id, 1_501).unwrap();
        assert_eq!(list_ids(&by_id_value), ["a", "aa"]);
        assert_eq!(
            by_id_value[0]["metadata"]["settings"],
            json!({"folderId": "folder-1"})
        );
        let catalog = request(
            "conversation.list",
            11,
            json!({
                "user_id": 11,
                "catalog_page": true,
                "include_messages": false,
                "limit": 2,
                "settings_keys": []
            }),
            None,
        );
        let first_page = execute(&mut database, &catalog, 1_502).unwrap();
        assert_eq!(first_page["total_count"], 4);
        assert_eq!(first_page["has_more"], true);
        assert_eq!(list_ids(&first_page["items"]), ["z", "b"]);
        assert_eq!(first_page["items"][0]["metadata"]["settings"], json!({}));
        let second_page = request(
            "conversation.list",
            11,
            json!({
                "user_id": 11,
                "catalog_page": true,
                "before_updated_at": 100,
                "before_id": "b",
                "limit": 2
            }),
            None,
        );
        let second_page = execute(&mut database, &second_page, 1_503).unwrap();
        assert_eq!(list_ids(&second_page["items"]), ["aa", "a"]);
        assert_eq!(second_page["has_more"], false);
        let folder_page = request(
            "conversation.list",
            11,
            json!({
                "user_id": 11,
                "catalog_page": true,
                "folder_id": "folder-1",
                "limit": 10
            }),
            None,
        );
        let folder_page = execute(&mut database, &folder_page, 1_504).unwrap();
        assert_eq!(folder_page["total_count"], 2);
        assert_eq!(list_ids(&folder_page["items"]), ["aa", "a"]);

        let update = request(
            "conversation.metadata.update",
            11,
            json!({
                "conv_id": "a",
                "user_id": 11,
                "updates": {"updated_at": 300}
            }),
            Some("rekey-index-a"),
        );
        execute(&mut database, &update, 2_000).unwrap();
        assert_eq!(indexed_ids(&database), ["a", "z", "b", "aa"]);
    }

    #[test]
    fn sidebar_catalog_survives_a_tombstone_dense_updated_index() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for n in 1..=4 {
            execute(
                &mut database,
                &request(
                    "conversation.create",
                    11,
                    json!({"conv_id": format!("catalog-{n}"), "user_id": 11,
                    "title": "Catalog", "settings": {}, "created_at": n * 10,
                    "updated_at": n * 10}),
                    Some(&format!("create-catalog-{n}")),
                ),
                1_000,
            )
            .unwrap();
        }
        // Retired covering-index keys left by historical metadata updates.
        // Primary headers stay small; tombstones remain for OCC, not as rows.
        for batch in 0..64 {
            let mut transaction = database.begin(7, 11).unwrap();
            for n in batch * 128..(batch + 1) * 128 {
                let mut raw = (!25_u64).to_be_bytes().to_vec();
                for byte in format!("retired-{n:0248}").bytes() {
                    raw.extend_from_slice(&[!byte, 0]);
                }
                raw.push(u8::MAX);
                let key = EntityKey::new(
                    7,
                    11,
                    crate::generated_tofudb_ir::CONVERSATION_UPDATED_INDEX_NAMESPACE,
                    &raw,
                )
                .unwrap();
                database.entity_delete(&mut transaction, key).unwrap();
            }
            database.commit(transaction).unwrap();
        }
        let page = execute(
            &mut database,
            &request(
                "conversation.list",
                11,
                json!({"user_id": 11, "catalog_page": true, "include_messages": false,
                "limit": 500, "settings_keys": []}),
                None,
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(page["total_count"], 4);
        assert_eq!(page["has_more"], false);
        let ids: Vec<_> = page["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["metadata"]["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, ["catalog-4", "catalog-3", "catalog-2", "catalog-1"]);
    }

    #[test]
    fn settled_turn_append_cow_write_footprint_stays_bounded_as_transcript_grows() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "append-footprint", "user_id": 11,
                    "title": "Turns", "settings": {}, "created_at": 1, "updated_at": 1}),
                Some("append-footprint-create"),
            ),
            1_000,
        )
        .unwrap();

        // Exercise the real receipted write path, not a synthetic single-key
        // append. Keep IDs and projection sizes fixed while the transcript and
        // all of its indexes grow. Count staged immutable blocks rather than
        // elapsed time: fsync latency is not a deterministic regression oracle.
        let mut early_bytes = 0;
        for index in 0..256 {
            let command_id = format!("append-footprint-{index:04}");
            let turn_id = format!("turn-{index:04}");
            let before = database.engine_mut_for_test().block_write_metrics();
            let result = execute(
                &mut database,
                &request(
                    "turn.append_settled",
                    11,
                    json!({"conversation_id": "append-footprint", "user_id": 11,
                        "command_id": command_id, "actor": "assistant",
                        "projection": {"content": "x".repeat(128)},
                        "created_at": index + 2, "turn_id": turn_id}),
                    Some(&command_id),
                ),
                2_000 + index * 1_000,
            )
            .unwrap();
            assert_eq!(result["turn"]["ordinal"], index);
            if index > 0 {
                assert_eq!(
                    result["turn"]["parentTurnId"],
                    format!("turn-{:04}", index - 1)
                );
            }
            let after = database.engine_mut_for_test().block_write_metrics();
            let blocks = after.blocks_staged - before.blocks_staged;
            let bytes = after.bytes_staged - before.bytes_staged;
            // These ceilings allow page splits and ancestor growth, but not
            // rewriting the accumulated transcript on each append.
            assert!(blocks <= 64, "append {index}: {blocks} staged blocks");
            assert!(bytes <= 512 * 1024, "append {index}: {bytes} staged bytes");
            if index == 31 {
                early_bytes = bytes;
            }
            if index == 255 {
                // Transcript-archive seal rounds (every SEAL_MIN_MESSAGES
                // appends) write one bounded transcript page + manifest —
                // a documented ~4 KiB/append amortized cost, not transcript
                // rewriting — so the growth comparison uses an ordinary
                // floor under the early baseline.
                let ordinary_baseline = early_bytes.max(4 * 1024);
                assert!(
                    bytes <= ordinary_baseline * 3,
                    "8x transcript growth: {early_bytes} -> {bytes} staged bytes"
                );
            }
            if matches!(index, 0 | 31 | 127 | 255) {
                eprintln!(
                    "append footprint turns={} blocks={blocks} bytes={bytes}",
                    index + 1
                );
            }
        }
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let turn = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({"conversation_id": "append-footprint", "user_id": 11,
                    "turn_id": "turn-0255"}),
                None,
            ),
            300_000,
        )
        .unwrap();
        assert_eq!(turn["ordinal"], 255);
        assert_eq!(turn["parentTurnId"], "turn-0254");
    }

    #[test]
    fn settled_turn_append_is_owner_scoped_ordered_receipted_and_blob_capable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[81; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        let create = request(
            "conversation.create",
            11,
            json!({
                "conv_id": "turn-conversation",
                "user_id": 11,
                "title": "Turns",
                "settings": {},
                "created_at": 1,
                "updated_at": 1
            }),
            Some("turn-conversation-create"),
        );
        execute(&mut database, &create, 1_000).unwrap();

        let first = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "command_id": "ingest-human",
                "actor": "human",
                "projection": {
                    "content": "question",
                    "timestamp": 2,
                    "role": "untrusted-duplicate",
                    "turnId": "untrusted-duplicate"
                },
                "created_at": 0,
                "turn_id": "turn-human"
            }),
            Some("turn-append-human"),
        );
        let first_result = execute(&mut database, &first, 2_000).unwrap();
        assert_eq!(first_result["turn"]["turnId"], "turn-human");
        assert_eq!(first_result["turn"]["ordinal"], 0);
        assert_eq!(first_result["turn"]["parentTurnId"], Value::Null);
        assert_eq!(
            first_result["turn"]["projection"],
            json!({"content": "question", "timestamp": 2})
        );
        assert_eq!(first_result["turn"]["createdAt"], 0);
        assert_eq!(first_result["attempt"], Value::Null);
        assert_eq!(first_result["conversationRevision"], 1);
        assert_eq!(execute(&mut database, &first, 9_000).unwrap(), first_result);

        let second = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "command_id": "ingest-assistant",
                "actor": "assistant",
                "kind": "fixture",
                "projection": {
                    "content": "x".repeat(20 * 1024),
                    "timestamp": 3,
                    "images": [
                        {
                            "base64": "aGVsbG8=",
                            "preview": "data:image/png;base64,aGVsbG8=",
                            "mediaType": " IMAGE/PNG "
                        },
                        {"preview": "data:image/webp;base64,d2VicA=="}
                    ]
                },
                "created_at": 3,
                "turn_id": "turn-assistant"
            }),
            Some("turn-append-assistant"),
        );
        let second_result = execute(&mut database, &second, 3_000).unwrap();
        assert_eq!(second_result["turn"]["ordinal"], 1);
        assert_eq!(second_result["turn"]["parentTurnId"], "turn-human");
        assert_eq!(
            second_result["turn"]["projection"]["content"]
                .as_str()
                .unwrap()
                .len(),
            20 * 1024
        );
        assert_eq!(second_result["attempt"]["operation"], "ingest");
        assert_eq!(second_result["conversationRevision"], 2);

        let activity = request(
            "conversation.activity_dates",
            11,
            json!({
                "user_id": 11,
                "updated_at_gte": 0,
                "day_boundaries_ms": [1, 3, 4]
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &activity, 3_100).unwrap(),
            json!({"candidate_count": 1, "counts": [1, 1]})
        );

        let exists = request(
            "turn.exists",
            11,
            json!({"conversation_id": "turn-conversation", "user_id": 11}),
            None,
        );
        assert_eq!(execute(&mut database, &exists, 4_000).unwrap(), json!(true));
        let revision = request(
            "turn.revision",
            11,
            json!({"conversation_id": "turn-conversation", "user_id": 11}),
            None,
        );
        assert_eq!(execute(&mut database, &revision, 4_000).unwrap(), json!(2));
        let list = request(
            "turn.list",
            11,
            json!({"conversation_id": "turn-conversation", "user_id": 11}),
            None,
        );
        let listed = execute(&mut database, &list, 4_000).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 2);
        assert_eq!(listed[0]["turnId"], "turn-human");
        assert_eq!(listed[1], second_result["turn"]);
        let delta = request(
            "turn.list_delta",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "since_ms": 5_002
            }),
            None,
        );
        let delta = execute(&mut database, &delta, 5_500).unwrap();
        assert_eq!(delta["turns"], json!([second_result["turn"]]));
        assert_eq!(delta["deletedTurnIds"], json!([]));
        assert_eq!(delta["serverNowMs"], 5_500);
        let deduplicated_delta = request(
            "turn.list_delta",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "since_ms": 5_002,
                "known_revisions": {"turn-assistant": 1}
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &deduplicated_delta, 5_600).unwrap(),
            json!({"turns": [], "deletedTurnIds": [], "serverNowMs": 5_600})
        );
        let get = request(
            "turn.get",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "turn_id": "turn-assistant"
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &get, 4_000).unwrap(),
            second_result["turn"]
        );
        let image = |owner, revision, index| {
            request(
                "turn.image.get",
                owner,
                json!({
                    "conversation_id": "turn-conversation",
                    "user_id": owner,
                    "turn_id": "turn-assistant",
                    "projection_revision": revision,
                    "image_index": index
                }),
                None,
            )
        };
        assert_eq!(
            execute(&mut database, &image(11, 1, 0), 4_000).unwrap(),
            json!({
                "stale": false,
                "projectionRevision": 1,
                "mediaType": "image/png",
                "base64": "aGVsbG8="
            })
        );
        assert_eq!(
            execute(&mut database, &image(11, 1, 1), 4_000).unwrap(),
            json!({
                "stale": false,
                "projectionRevision": 1,
                "mediaType": "image/webp",
                "base64": "d2VicA=="
            })
        );
        assert_eq!(
            execute(&mut database, &image(11, 2, 0), 4_000).unwrap(),
            json!({"stale": true, "projectionRevision": 1})
        );
        assert_eq!(
            execute(&mut database, &image(11, 1, 2), 4_000).unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(&mut database, &image(12, 1, 0), 4_000).unwrap(),
            Value::Null
        );

        let wrong_owner = request(
            "turn.get",
            12,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 12,
                "turn_id": "turn-assistant"
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &wrong_owner, 4_000).unwrap(),
            Value::Null
        );
        let other_create = request(
            "conversation.create",
            12,
            json!({
                "conv_id": "other-owner-conversation",
                "user_id": 12,
                "title": "Other",
                "settings": {},
                "created_at": 1,
                "updated_at": 1
            }),
            Some("other-owner-conversation-create"),
        );
        execute(&mut database, &other_create, 4_100).unwrap();
        let cross_owner_turn_collision = request(
            "turn.append_settled",
            12,
            json!({
                "conversation_id": "other-owner-conversation",
                "user_id": 12,
                "command_id": "cross-owner-turn-collision",
                "actor": "human",
                "projection": {"content": "must not commit"},
                "turn_id": "turn-human"
            }),
            Some("cross-owner-turn-collision"),
        );
        assert_eq!(
            execute(&mut database, &cross_owner_turn_collision, 4_200).unwrap_err(),
            SemanticExecutionError::Conflict
        );
        let mut attempt_collision = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        let existing_attempt_id = second_result["attempt"]["attemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        assert_eq!(
            crate::turn::append_settled(
                &database,
                &mut attempt_collision,
                &crate::turn::AppendSettledRequest {
                    conversation_id: "other-owner-conversation".to_owned(),
                    actor: "assistant".to_owned(),
                    status: "completed".to_owned(),
                    projection_json: b"{}".to_vec(),
                    projection_timestamp: None,
                                        settlement_json: b"{}".to_vec(),
                    lane_id: "main".to_owned(),
                    command_id: "cross-owner-attempt-collision".to_owned(),
                    kind: "fixture".to_owned(),
                    run_id: String::new(),
                    turn_id: "unique-other-owner-turn".to_owned(),
                    attempt_id: Some(existing_attempt_id),
                    created_at_ms: 4_300,
                    committed_at_ms: 4_300,
                    defaults: crate::conversation_header::TurnDefaults {
                        allow_create: false,
                        title: "unused".to_owned(),
                        settings_json: b"{}".to_vec(),
                        created_at_ms: 4_300,
                    },
                },
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::WouldBlock
        );
        let changed_replay = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "command_id": "ingest-human",
                "actor": "human",
                "projection": {"content": "changed"},
                "turn_id": "turn-human"
            }),
            Some("turn-append-human"),
        );
        assert_eq!(
            execute(&mut database, &changed_replay, 5_000).unwrap_err(),
            SemanticExecutionError::Conflict
        );

        let conversation_get = request(
            "conversation.get",
            11,
            json!({"conv_id": "turn-conversation", "user_id": 11}),
            None,
        );
        let projected = execute(&mut database, &conversation_get, 5_000).unwrap();
        assert_eq!(projected["metadata"]["msg_count"], 2);
        assert_eq!(projected["messages"][0]["role"], "user");
        assert_eq!(projected["messages"][0]["_turnId"], "turn-human");
        assert_eq!(projected["messages"][1]["role"], "assistant");
        assert_eq!(projected["messages"][1]["_turnId"], "turn-assistant");
        let windowed_get = request(
            "conversation.get",
            11,
            json!({
                "conv_id": "turn-conversation",
                "user_id": 11,
                "message_window": 1,
                "before_sequence": 1
            }),
            None,
        );
        let windowed = execute(&mut database, &windowed_get, 5_000).unwrap();
        assert_eq!(
            windowed["message_page"],
            json!({"total_count": 2, "start": 0, "end": 1})
        );
        assert_eq!(windowed["messages"][0]["_turnId"], "turn-human");
        let conversation_list = request(
            "conversation.list",
            11,
            json!({
                "user_id": 11,
                "ids": ["turn-conversation"],
                "include_messages": true
            }),
            None,
        );
        let projected_list = execute(&mut database, &conversation_list, 5_000).unwrap();
        assert_eq!(projected_list[0]["messages"], projected["messages"]);

        let attempt_get = request(
            "turn.attempt.get",
            11,
            json!({
                "attempt_id": second_result["attempt"]["attemptId"],
                "user_id": 11
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &attempt_get, 5_000).unwrap(),
            second_result["attempt"]
        );
        let foreign_attempt_get = request(
            "turn.attempt.get",
            12,
            json!({
                "attempt_id": second_result["attempt"]["attemptId"],
                "user_id": 12
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &foreign_attempt_get, 5_000).unwrap(),
            Value::Null
        );
        let snapshot = request(
            "turn.sync.snapshot",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "turn_limit": 1,
                "include_artifact_hint": true
            }),
            None,
        );
        let snapshot = execute(&mut database, &snapshot, 5_000).unwrap();
        assert_eq!(snapshot["conversationRevision"], 2);
        assert_eq!(snapshot["syncSequence"], 2);
        assert_eq!(snapshot["turns"], json!([second_result["turn"]]));
        assert_eq!(snapshot["attempts"], json!([second_result["attempt"]]));
        assert_eq!(snapshot["queueItems"], json!([]));
        assert_eq!(snapshot["hasArtifacts"], false);
        assert_eq!(
            snapshot["turnWindow"],
            json!({"laneId": "main", "nextBeforeOrdinal": 1, "hasMore": true, "totalTurns": 2})
        );
        let changes = request(
            "turn.sync.changes",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "after": 0,
                "limit": 1
            }),
            None,
        );
        let changes = execute(&mut database, &changes, 5_000).unwrap();
        assert_eq!(changes["head"], 2);
        assert_eq!(changes["events"].as_array().unwrap().len(), 1);
        assert_eq!(changes["events"][0]["syncSeq"], 1);
        assert_eq!(changes["events"][0]["turnId"], "turn-human");
        assert!(changes["events"][0].get("attemptId").is_none());
        assert_eq!(
            changes["events"][0]["payload"]["turns"][0],
            first_result["turn"]
        );
        assert_eq!(changes["hasMore"], true);
        assert_eq!(changes["resetRequired"], false);
        let second_change = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "turn-conversation",
                    "user_id": 11,
                    "after": 1,
                    "limit": 1
                }),
                None,
            ),
            5_000,
        )
        .unwrap();
        assert_eq!(second_change["events"][0]["turnId"], "turn-assistant");
        assert_eq!(
            second_change["events"][0]["attemptId"],
            second_result["attempt"]["attemptId"]
        );
        let page = request(
            "turn.sync.page",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "lane_id": "main",
                "sync_sequence": 2,
                "limit": 1
            }),
            None,
        );
        let page = execute(&mut database, &page, 5_000).unwrap();
        assert_eq!(page["turns"], json!([second_result["turn"]]));
        assert_eq!(page["attempts"], json!([second_result["attempt"]]));
        assert_eq!(page["hasMore"], true);
        assert_eq!(page["totalTurns"], 2);

        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(execute(&mut reopened, &list, 6_000).unwrap(), listed);
        assert_eq!(execute(&mut reopened, &revision, 6_000).unwrap(), json!(2));

        let branch = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "command_id": "ingest-branch",
                "actor": "assistant",
                "lane_id": "branch-a",
                "projection": {"content": "alternate"},
                "created_at": 4,
                "turn_id": "turn-branch"
            }),
            Some("turn-append-branch"),
        );
        let branch = execute(&mut reopened, &branch, 6_100).unwrap();
        assert_eq!(branch["turn"]["ordinal"], 0);
        assert_eq!(branch["turn"]["parentTurnId"], Value::Null);
        assert_eq!(branch["conversationRevision"], 3);
        let main_document = execute(&mut reopened, &conversation_get, 6_200).unwrap();
        assert_eq!(main_document["metadata"]["msg_count"], 2);
        assert_eq!(main_document["messages"].as_array().unwrap().len(), 2);
        let branch_list = request(
            "turn.list",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "lane_id": "branch-a"
            }),
            None,
        );
        assert_eq!(
            execute(&mut reopened, &branch_list, 6_200).unwrap(),
            json!([branch["turn"]])
        );
        let branched_snapshot = request(
            "turn.sync.snapshot",
            11,
            json!({
                "conversation_id": "turn-conversation",
                "user_id": 11,
                "turn_limit": 1
            }),
            None,
        );
        let branched_snapshot = execute(&mut reopened, &branched_snapshot, 6_200).unwrap();
        assert_eq!(branched_snapshot["turns"].as_array().unwrap().len(), 3);
        assert!(branched_snapshot.get("turnWindow").is_none());
    }

    #[test]
    fn turn_attempt_create_is_atomic_owner_scoped_and_lost_ack_replayable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "attempt-create-conversation",
                    "user_id": 11,
                    "title": "Attempt create",
                    "settings": {}
                }),
                Some("attempt-create-conversation-command"),
            ),
            1_000,
        )
        .unwrap();
        let settled = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "attempt-create-conversation",
                    "user_id": 11,
                    "command_id": "attempt-create-seed",
                    "actor": "assistant",
                    "kind": "reply",
                    "projection": {
                        "content": "partial answer",
                        "thinking": "private tail",
                        "segments": [],
                        "toolRounds": []
                    },
                    "settlement": {
                        "outcome": "completed",
                        "cause": "fixture",
                        "resumeOptions": [{"operation": "continue", "anchor": {}}]
                    },
                    "turn_id": "attempt-create-turn"
                }),
                Some("attempt-create-seed-receipt"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(settled["turn"]["projectionRevision"], 1);

        let create = request(
            "turn.attempt.create",
            11,
            json!({
                "conversation_id": "attempt-create-conversation",
                "turn_id": "attempt-create-turn",
                "user_id": 11,
                "command_id": "attempt-create-idempotency",
                "operation": "regenerate",
                "dispatch_mode": "conversation_executor",
                "target_actor": "assistant",
                "target_kind": "reply",
                "expected_projection_revision": 1,
                "config": {"temperature": 0.25}
            }),
            Some("attempt-create-receipt"),
        );
        let created = execute(&mut database, &create, 3_000).unwrap();
        assert_eq!(created["turn"]["status"], "pending");
        assert_eq!(created["turn"]["projectionRevision"], 2);
        assert_eq!(created["turn"]["projection"]["content"], "");
        assert_eq!(created["turn"]["projection"]["thinking"], "");
        assert_eq!(created["attempt"]["operation"], "regenerate");
        assert_eq!(created["attempt"]["status"], "pending");
        assert_eq!(
            created["attempt"]["commandId"],
            "attempt-create-idempotency"
        );
        assert!(created["attempt"].get("_config").is_none());
        assert_eq!(created["conversationRevision"], 2);
        assert_eq!(created["streamCursor"], 1);
        assert_eq!(created["idempotentReplay"], false);
        assert_eq!(created["_needsStart"], true);

        let mut replay_request = create.clone();
        replay_request.command_id = Some("attempt-create-replay-receipt".to_owned());
        let replay = execute(&mut database, &replay_request, 9_000).unwrap();
        assert_eq!(replay["attempt"], created["attempt"]);
        assert_eq!(replay["turn"], created["turn"]);
        assert_eq!(replay["conversationRevision"], 2);
        assert_eq!(replay["idempotentReplay"], true);

        let foreign = request(
            "turn.attempt.create",
            12,
            json!({
                "conversation_id": "attempt-create-conversation",
                "turn_id": "attempt-create-turn",
                "user_id": 12,
                "command_id": "foreign-attempt-create",
                "operation": "regenerate",
                "target_actor": "assistant",
                "target_kind": "reply",
                "expected_projection_revision": 1
            }),
            Some("foreign-attempt-create-receipt"),
        );
        assert_eq!(
            execute(&mut database, &foreign, 4_000).unwrap_err(),
            SemanticExecutionError::NotFound
        );
    }

    #[test]
    fn resume_context_rebind_ir_rejects_content_and_oversized_snapshots() {
        let request = request("turn.attempt.create", 11, json!({
            "conversation_id": "resume-context", "turn_id": "reply", "user_id": 11,
            "command_id": "resume-context", "operation": "continue",
            "expected_projection_revision": 1,
            "input_update": {"contextSnapshot": null}
        }), Some("resume-context-receipt"));
        let admitted = admit_request(&request, NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION,
        }, AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 1_000).unwrap();
        let mut ir = compile_turn(admitted, 1_000).unwrap();
        assert!(ir.validate().is_ok());
        for update in [
            json!({"content": "changed"}),
            json!({"contextSnapshot": {"snapshot": {"model": "x".repeat(17 * 1024)}}}),
            json!({"contextSnapshot": null, "content": "changed"}),
        ] {
            match &mut ir.steps[0] {
                TransactionIrStep::TurnAttemptCreate { request, .. } => {
                    request.input_update = Some(update.as_object().unwrap().clone());
                }
                _ => panic!("expected attempt-create transaction"),
            }
            assert!(ir.validate().is_err());
        }
    }

    #[test]
    fn resume_attempt_create_rebinds_context_snapshot_without_rewriting_submitted() {
        // Pause → switch-model → continue refreshes ONLY the submitted rail:
        // the contextSnapshot rebind applies surgically onto the CURRENT
        // parent projection (legacy `_ctx` dropped, no client revision
        // fence), while any content edit stays regenerate-only. Mirrors the
        // sidecar contract locked by
        // tests/turn_lifecycle/test_turn_lifecycle.py::test_resume_ctx_only_input_update_rebinds_submitted_rail.
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "resume-ctx-conversation",
                    "user_id": 11,
                    "command_id": "resume-ctx-pair",
                    "input_projection": {"content": "hello"},
                    "conversation_defaults": {"allowCreate": true},
                    "now": 1_000
                }),
                Some("resume-ctx-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let turn_id = pair["turn"]["turnId"].as_str().unwrap().to_owned();
        let parent_id = pair["submittedTurn"]["turnId"].as_str().unwrap().to_owned();
        let interrupted = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": pair["attempt"]["attemptId"],
                    "user_id": 11,
                    "terminal": true,
                    "status": "interrupted",
                    "settlement": {
                        "outcome": "interrupted",
                        "cause": "user",
                        "resumeOptions": [{"operation": "continue", "anchor": {}}]
                    },
                    "now": 2_000
                }),
                Some("resume-ctx-settle"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(interrupted["applied"], true);
        // Seed the legacy `_ctx` rail the rebind must drop.
        execute(
            &mut database,
            &request(
                "turn.projection.update",
                11,
                json!({
                    "conversation_id": "resume-ctx-conversation",
                    "turn_id": parent_id,
                    "user_id": 11,
                    "projection": {"content": "hello", "_ctx": {"model": "gpt-4o"}},
                    "expected_projection_revision": 1,
                    "updated_at": 2_500
                }),
                Some("resume-ctx-seed-ctx"),
            ),
            2_500,
        )
        .unwrap();
        let settled = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "resume-ctx-conversation",
                    "turn_id": turn_id,
                    "user_id": 11
                }),
                None,
            ),
            2_600,
        )
        .unwrap();

        // A content edit stays regenerate-only.
        let edit = request(
            "turn.attempt.create",
            11,
            json!({
                "conversation_id": "resume-ctx-conversation",
                "turn_id": turn_id,
                "user_id": 11,
                "command_id": "resume-ctx-edit",
                "operation": "continue",
                "expected_projection_revision": settled["projectionRevision"],
                "input_update": {"content": "rewritten"}
            }),
            Some("resume-ctx-edit-receipt"),
        );
        assert_eq!(
            execute(&mut database, &edit, 3_000).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );

        // An oversized snapshot is refused outright.
        let oversized = request(
            "turn.attempt.create",
            11,
            json!({
                "conversation_id": "resume-ctx-conversation",
                "turn_id": turn_id,
                "user_id": 11,
                "command_id": "resume-ctx-oversized",
                "operation": "continue",
                "expected_projection_revision": settled["projectionRevision"],
                "input_update": {"contextSnapshot": {
                    "blockId": "turn-context",
                    "snapshot": {"model": "x".repeat(17 * 1024)}
                }}
            }),
            Some("resume-ctx-oversized-receipt"),
        );
        assert_eq!(
            execute(&mut database, &oversized, 3_000).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );

        // The rail rebind merges into the CURRENT parent projection; a stale
        // expected_input_projection_revision is display-state, never a fence.
        let rail = json!({
            "blockId": "turn-context",
            "snapshot": {"model": "kimi-k3", "toolsLabel": "Tools"}
        });
        let resumed = execute(
            &mut database,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "resume-ctx-conversation",
                    "turn_id": turn_id,
                    "user_id": 11,
                    "command_id": "resume-ctx-continue",
                    "operation": "continue",
                    "expected_projection_revision": settled["projectionRevision"],
                    "expected_input_projection_revision": 0,
                    "input_update": {"contextSnapshot": rail}
                }),
                Some("resume-ctx-continue-receipt"),
            ),
            3_100,
        )
        .unwrap();
        assert_eq!(resumed["turn"]["status"], "pending");
        let submitted = &resumed["submittedTurn"];
        assert_eq!(submitted["projection"]["content"], "hello");
        assert_eq!(submitted["projection"]["contextSnapshot"], rail);
        assert!(submitted["projection"].get("_ctx").is_none());
        assert_eq!(submitted["projectionRevision"], 3);

        // A null contextSnapshot clears the rail on the next resume.
        let interrupted_again = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": resumed["attempt"]["attemptId"],
                    "user_id": 11,
                    "terminal": true,
                    "status": "interrupted",
                    "settlement": {
                        "outcome": "interrupted",
                        "cause": "user",
                        "resumeOptions": [{"operation": "continue", "anchor": {}}]
                    },
                    "now": 4_000
                }),
                Some("resume-ctx-settle-again"),
            ),
            4_000,
        )
        .unwrap();
        assert_eq!(interrupted_again["applied"], true);
        let settled_again = execute(
            &mut database,
            &request("turn.get", 11, json!({
                "conversation_id": "resume-ctx-conversation", "turn_id": turn_id,
                "user_id": 11
            }), None), 4_050,
        ).unwrap();
        let cleared = execute(
            &mut database,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "resume-ctx-conversation",
                    "turn_id": turn_id,
                    "user_id": 11,
                    "command_id": "resume-ctx-clear",
                    "operation": "continue",
                    "expected_projection_revision": settled_again["projectionRevision"],
                    "input_update": {"contextSnapshot": Value::Null}
                }),
                Some("resume-ctx-clear-receipt"),
            ),
            4_100,
        )
        .unwrap();
        let cleared_submitted = &cleared["submittedTurn"];
        assert_eq!(cleared_submitted["projection"]["content"], "hello");
        assert!(cleared_submitted["projection"].get("contextSnapshot").is_none());
        assert_eq!(cleared_submitted["projectionRevision"], 4);
    }

    #[test]
    fn conversation_attempt_worker_dispatch_is_global_ordered_atomic_and_replayable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let ((later, _later_source), (earlier, earlier_source)) = {
            let mut create_dispatchable =
                |owner_user_id: u64, conversation_id: &str, turn_id: &str, created_at_ms: u64| {
                    let projection = if owner_user_id == 11 {
                        let attempt_id = deterministic_attempt_id_for_test(
                            conversation_id,
                            turn_id,
                            &format!("create-{turn_id}"),
                        );
                        json!({
                            "content": "partial",
                            "timingTrace": {
                                "version": 1,
                                "taskId": format!("conversation-attempt:{attempt_id}"),
                                "status": "running",
                                "running": true
                            }
                        })
                    } else {
                        json!({"content": "partial"})
                    };
                    let seed = execute(
                        &mut database,
                        &request(
                            "turn.append_settled",
                            owner_user_id,
                            json!({
                                "conversation_id": conversation_id,
                                "user_id": owner_user_id,
                                "command_id": format!("seed-{turn_id}"),
                                "actor": "assistant",
                                "kind": "reply",
                                "projection": projection,
                                "settlement": {
                                    "outcome": "completed",
                                    "resumeOptions": [{"operation": "continue", "anchor": {}}]
                                },
                                "turn_id": turn_id,
                                "conversation_defaults": {
                                    "allowCreate": true,
                                    "title": "Dispatch",
                                    "settings": {}
                                }
                            }),
                            Some(&format!("seed-receipt-{turn_id}")),
                        ),
                        created_at_ms - 1,
                    )
                    .unwrap();
                    let accepted = execute(
                        &mut database,
                        &request(
                            "turn.attempt.create",
                            owner_user_id,
                            json!({
                                "conversation_id": conversation_id,
                                "turn_id": turn_id,
                                "user_id": owner_user_id,
                                "command_id": format!("create-{turn_id}"),
                                "operation": "continue",
                                "dispatch_mode": "conversation_executor",
                                "expected_projection_revision": 1,
                                "config": {"model": format!("model-{owner_user_id}")}
                            }),
                            Some(&format!("create-receipt-{turn_id}")),
                        ),
                        created_at_ms,
                    )
                    .unwrap();
                    (accepted, seed["attempt"]["attemptId"].clone())
                };
            (
                create_dispatchable(11, "dispatch-a", "turn-a", 3_000),
                create_dispatchable(12, "dispatch-b", "turn-b", 2_000),
            )
        };

        let list = request(
            "turn.attempt.dispatchable.list",
            99,
            json!({"created_before_ms": 3_000, "limit": 32}),
            None,
        );
        let listed = execute(&mut database, &list, 3_100).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 2);
        assert_eq!(listed[0]["userId"], 12);
        assert_eq!(listed[0]["attempt"], earlier["attempt"]);
        assert_eq!(listed[0]["config"], json!({
            "model": "model-12",
            "_resumeSource": {
                "attemptId": earlier_source,
                "taskId": "",
                "status": "completed",
                "cause": "ingested"
            }
        }));
        assert_eq!(listed[1]["userId"], 11);
        assert_eq!(listed[1]["turn"], later["turn"]);

        let attempt_id = later["attempt"]["attemptId"].as_str().unwrap();
        let dispatch = request(
            "turn.attempt.dispatch_worker",
            11,
            json!({
                "attempt_id": attempt_id,
                "user_id": 11,
                "principal": {
                    "kind": "user",
                    "subject_id": "user-11",
                    "owner_user_id": 11,
                    "tenant_id": "tenant-seven",
                    "scopes": ["chat:read", "chat:write"]
                },
                "priority": 7,
                "now_ms": 3_200
            }),
            Some("dispatch-worker-command"),
        );
        let dispatched = execute(&mut database, &dispatch, 3_200).unwrap();
        assert_eq!(dispatched["created"], true);
        assert_eq!(
            dispatched["attempt"]["taskId"],
            format!("conversation-attempt:{attempt_id}")
        );
        assert_eq!(dispatched["job"]["payload"]["attemptId"], attempt_id);
        assert_eq!(
            dispatched["job"]["payload"]["principal"]["owner_user_id"],
            11
        );
        assert_eq!(dispatched["job"]["priority"], 7);

        let remaining = execute(&mut database, &list, 3_300).unwrap();
        assert_eq!(remaining.as_array().unwrap().len(), 1);
        assert_eq!(remaining[0]["userId"], 12);

        let replay = execute(&mut database, &dispatch, 3_400).unwrap();
        assert_eq!(replay["created"], false);
        assert_eq!(replay["idempotentReplay"], true);
        assert_eq!(replay["attempt"], dispatched["attempt"]);
        assert_eq!(replay["job"], dispatched["job"]);

        let task_id = dispatched["attempt"]["taskId"].as_str().unwrap();
        let timing_list = execute(
            &mut database,
            &request(
                "turn.timing_trace.list",
                11,
                json!({"conversation_id": "dispatch-a", "user_id": 11, "limit": 10}),
                None,
            ),
            3_403,
        )
        .unwrap();
        assert_eq!(timing_list["has_more"], false);
        assert_eq!(timing_list["records"].as_array().unwrap().len(), 1);
        assert_eq!(timing_list["records"][0]["attempt_id"], attempt_id);
        assert_eq!(timing_list["records"][0]["task_id"], task_id);
        assert_eq!(timing_list["records"][0]["created_at"], 3_000);
        assert_eq!(timing_list["records"][0]["settled_at"], Value::Null);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.timing_trace.list",
                    11,
                    json!({
                        "conversation_id": "dispatch-a",
                        "user_id": 11,
                        "before_created_at": 3_000
                    }),
                    None,
                ),
                3_404,
            )
            .unwrap(),
            json!({"records": [], "has_more": false})
        );
        let timing = execute(
            &mut database,
            &request(
                "turn.timing_trace.get",
                11,
                json!({"task_id": task_id, "user_id": 11}),
                None,
            ),
            3_405,
        )
        .unwrap();
        assert_eq!(timing["attemptId"], attempt_id);
        assert_eq!(timing["attemptStatus"], "pending");
        assert_eq!(timing["turnId"], "turn-a");
        assert_eq!(timing["timingTrace"]["taskId"], task_id);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.timing_trace.get",
                    12,
                    json!({"task_id": task_id, "user_id": 12}),
                    None,
                ),
                3_406,
            )
            .unwrap(),
            Value::Null
        );

        let wrong_principal = request(
            "turn.attempt.dispatch_worker",
            12,
            json!({
                "attempt_id": earlier["attempt"]["attemptId"],
                "user_id": 12,
                "principal": {
                    "kind": "user",
                    "subject_id": "wrong-owner",
                    "owner_user_id": 11,
                    "tenant_id": null,
                    "scopes": []
                },
                "now_ms": 3_500
            }),
            Some("wrong-principal-command"),
        );
        assert_eq!(
            execute(&mut database, &wrong_principal, 3_500).unwrap_err(),
            SemanticExecutionError::Conflict
        );
        assert_eq!(execute(&mut database, &list, 3_600).unwrap(), remaining);
    }

    #[test]
    fn settled_turn_append_can_atomically_create_its_conversation_defaults() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[82; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
            .unwrap();
        let append = request(
            "turn.append_settled",
            21,
            json!({
                "conversation_id": "created-with-turn",
                "user_id": 21,
                "command_id": "created-with-turn-command",
                "actor": "human",
                "projection": {"text": "hello"},
                "turn_id": "created-with-turn-id",
                "conversation_defaults": {
                    "allowCreate": true,
                    "title": "Imported",
                    "createdAt": 7,
                    "settings": {"model": "m1", "activeTaskId": "must-not-persist"}
                }
            }),
            Some("created-with-turn-receipt"),
        );
        assert_eq!(
            execute(&mut database, &append, 8_000).unwrap()["conversationRevision"],
            1
        );
        let get = request(
            "conversation.get",
            21,
            json!({"conv_id": "created-with-turn", "user_id": 21}),
            None,
        );
        let conversation = execute(&mut database, &get, 9_000).unwrap();
        assert_eq!(conversation["metadata"]["title"], "Imported");
        assert_eq!(conversation["metadata"]["created_at"], 7);
        assert_eq!(conversation["metadata"]["updated_at"], 8_000);
        assert_eq!(conversation["metadata"]["msg_count"], 1);
        assert_eq!(conversation["metadata"]["rev"], 1);
        assert_eq!(conversation["metadata"]["settings"], json!({"model": "m1"}));
    }

    #[test]
    fn turn_delete_removes_branch_closure_and_publishes_delta_atomically() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (command_id, actor, lane_id, turn_id, projection) in [
            (
                "append-parent",
                "human",
                "main",
                "parent",
                json!({"content": "parent", "_branchLanes": [{"laneId": "branch-a"}]}),
            ),
            (
                "append-child",
                "assistant",
                "branch-a",
                "branch-child",
                json!({"content": "child"}),
            ),
            (
                "append-tail",
                "human",
                "main",
                "tail",
                json!({"content": "tail"}),
            ),
        ] {
            let append = request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "command_id": command_id,
                    "actor": actor,
                    "lane_id": lane_id,
                    "projection": projection,
                    "turn_id": turn_id,
                    "conversation_defaults": {
                        "allowCreate": true,
                        "title": "Delete",
                        "settings": {}
                    }
                }),
                Some(command_id),
            );
            execute(&mut database, &append, 1_000).unwrap();
        }
        let encode_text = |output: &mut Vec<u8>, value: &str| {
            output.extend_from_slice(&(value.len() as u16).to_be_bytes());
            output.extend_from_slice(value.as_bytes());
        };
        let mut conversation_prefix = Vec::new();
        encode_text(&mut conversation_prefix, "delete-conversation");
        let mut ancient_tombstone = conversation_prefix.clone();
        ancient_tombstone.extend_from_slice(&1_u64.to_be_bytes());
        ancient_tombstone.extend_from_slice(b"ancient");
        let mut ancient_age = 1_u64.to_be_bytes().to_vec();
        encode_text(&mut ancient_age, "delete-conversation");
        encode_text(&mut ancient_age, "ancient");
        let mut stale = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for key in [
            EntityKey::new(
                7,
                11,
                crate::generated_tofudb_ir::TURN_TOMBSTONE_NAMESPACE,
                &ancient_tombstone,
            )
            .unwrap(),
            EntityKey::new(
                7,
                11,
                crate::generated_tofudb_ir::TURN_TOMBSTONE_AGE_INDEX_NAMESPACE,
                &ancient_age,
            )
            .unwrap(),
        ] {
            database.entity_put(&mut stale, key, Vec::new()).unwrap();
        }
        database
            .entity_put(
                &mut stale,
                EntityKey::new(
                    7,
                    crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
                    crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
                    b"ancient",
                )
                .unwrap(),
                11_u64.to_be_bytes().to_vec(),
            )
            .unwrap();
        database.commit(stale).unwrap();
        database
            .configure_logical_outbox(&[83; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        let child_attempt = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "turn_id": "branch-child"
                }),
                None,
            ),
            1_100,
        )
        .unwrap()["currentAttemptId"]
            .as_str()
            .unwrap()
            .to_owned();
        let delete = request(
            "turn.delete",
            11,
            json!({
                "conversation_id": "delete-conversation",
                "user_id": 11,
                "turn_ids": ["parent"]
            }),
            Some("delete-parent"),
        );
        let delete_now = 7 * 24 * 60 * 60 * 1_000 + 2_000;
        assert_eq!(
            execute(&mut database, &delete, delete_now).unwrap(),
            json!({
                "deletedTurnIds": ["branch-child", "parent"],
                "conversationRevision": 4
            })
        );
        let remaining = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id": "delete-conversation", "user_id": 11}),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(remaining.as_array().unwrap().len(), 1);
        assert_eq!(remaining[0]["turnId"], "tail");
        let conversation = execute(
            &mut database,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "delete-conversation", "user_id": 11}),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(conversation["metadata"]["rev"], 4);
        assert_eq!(conversation["metadata"]["msg_count"], 1);
        assert_eq!(conversation["messages"].as_array().unwrap().len(), 1);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "turn.attempt.get",
                    11,
                    json!({"attempt_id": child_attempt, "user_id": 11}),
                    None,
                ),
                2_002,
            )
            .unwrap(),
            Value::Null
        );
        let delta = execute(
            &mut database,
            &request(
                "turn.list_delta",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "since_ms": 0
                }),
                None,
            ),
            2_003,
        )
        .unwrap();
        assert_eq!(delta["turns"], json!([remaining[0]]));
        assert_eq!(delta["deletedTurnIds"], json!(["branch-child", "parent"]));
        let mut claim_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let ancient_claim = EntityKey::new(
            7,
            crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
            b"ancient",
        )
        .unwrap();
        assert!(database
            .entity_get(&mut claim_read, &ancient_claim)
            .unwrap()
            .is_none());
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "after": 3
                }),
                None,
            ),
            2_004,
        )
        .unwrap();
        assert_eq!(changes["head"], 4);
        assert_eq!(changes["events"][0]["type"], "turn.deleted");
        assert_eq!(
            changes["events"][0]["payload"]["deletedTurnIds"],
            json!(["branch-child", "parent"])
        );
        let empty_branch_page = execute(
            &mut database,
            &request(
                "turn.sync.page",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "lane_id": "branch-a",
                    "sync_sequence": 4
                }),
                None,
            ),
            2_005,
        )
        .unwrap();
        assert_eq!(empty_branch_page["totalTurns"], 0);
        assert_eq!(empty_branch_page["turns"], json!([]));
        let recreated_branch = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "command_id": "append-recreated-branch",
                    "actor": "assistant",
                    "lane_id": "branch-a",
                    "projection": {"content": "recreated branch"},
                    "turn_id": "recreated-branch"
                }),
                Some("append-recreated-branch"),
            ),
            delete_now + 50,
        )
        .unwrap();
        assert_eq!(recreated_branch["turn"]["ordinal"], 1);
        assert_eq!(recreated_branch["turn"]["parentTurnId"], Value::Null);
        let after_delete = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "command_id": "append-after-delete",
                    "actor": "human",
                    "projection": {"content": "after delete"},
                    "turn_id": "after-delete"
                }),
                Some("append-after-delete"),
            ),
            delete_now + 100,
        )
        .unwrap();
        assert_eq!(after_delete["turn"]["ordinal"], 2);
        assert_eq!(after_delete["turn"]["parentTurnId"], "tail");
        let main_page = execute(
            &mut database,
            &request(
                "turn.sync.page",
                11,
                json!({
                    "conversation_id": "delete-conversation",
                    "user_id": 11,
                    "lane_id": "main",
                    "sync_sequence": 6
                }),
                None,
            ),
            delete_now + 101,
        )
        .unwrap();
        assert_eq!(main_page["totalTurns"], 2);
        assert_eq!(
            main_page["turns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|turn| turn["ordinal"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![1, 2]
        );
    }

    #[test]
    fn turn_projection_update_cas_rekeys_lane_delta_and_compact_replay_atomically() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[84; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "edit-conversation",
                    "user_id": 11,
                    "command_id": "append-editable",
                    "actor": "assistant",
                    "projection": {
                        "content": "hello",
                        "segments": [{"text": "hello"}],
                        "toolRounds": []
                    },
                    "turn_id": "editable",
                    "conversation_defaults": {
                        "allowCreate": true,
                        "title": "Edit",
                        "settings": {}
                    }
                }),
                Some("append-editable"),
            ),
            1_000,
        )
        .unwrap();
        let update = request(
            "turn.projection.update",
            11,
            json!({
                "conversation_id": "edit-conversation",
                "user_id": 11,
                "turn_id": "editable",
                "expected_projection_revision": 1,
                "projection": {
                    "content": "hello!",
                    "segments": [{"text": "hello!"}],
                    "toolRounds": [{"toolCallId": "call-1", "status": "done"}],
                    "role": "forged",
                    "provider_id": "provider-a"
                }
            }),
            Some("update-editable"),
        );
        let updated = execute(&mut database, &update, 2_000).unwrap();
        assert_eq!(updated["conversationRevision"], 2);
        assert_eq!(updated["turn"]["projectionRevision"], 2);
        assert_eq!(updated["turn"]["projection"]["content"], "hello!");
        assert_eq!(updated["turn"]["projection"]["providerId"], "provider-a");
        assert!(updated["turn"]["projection"].get("role").is_none());

        let lane = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({
                    "conversation_id": "edit-conversation",
                    "user_id": 11,
                    "lane_id": "main"
                }),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(lane[0], updated["turn"]);
        let delta = execute(
            &mut database,
            &request(
                "turn.list_delta",
                11,
                json!({
                    "conversation_id": "edit-conversation",
                    "user_id": 11,
                    "since_ms": 1_500,
                    "known_revisions": {"editable": 1}
                }),
                None,
            ),
            2_002,
        )
        .unwrap();
        assert_eq!(delta["turns"], json!([updated["turn"]]));
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "edit-conversation",
                    "user_id": 11,
                    "after": 1
                }),
                None,
            ),
            2_003,
        )
        .unwrap();
        assert_eq!(changes["head"], 2);
        assert_eq!(changes["events"][0]["type"], "turn.patch");
        assert_eq!(changes["events"][0]["turnId"], "editable");
        assert!(changes["events"][0].get("attemptId").is_none());
        assert_eq!(
            changes["events"][0]["payload"]["turnPatches"][0]["projectionPatch"]["operations"],
            json!([
                {"op": "set", "path": ["providerId"], "value": "provider-a"},
                {"op": "append_text", "path": ["content"], "value": "!"},
                {"op": "append_text", "path": ["segments", 0, "text"], "value": "!"},
                {"op": "append", "path": ["toolRounds"], "value": [{"toolCallId": "call-1", "status": "done"}]}
            ])
        );
        assert!(changes["events"][0]["payload"].get("turns").is_none());
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );
        assert_eq!(
            execute(&mut database, &update, 2_100).unwrap_err(),
            SemanticExecutionError::TurnProjectionStale
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );
    }

    #[test]
    fn turn_branch_lifecycle_removes_nested_lane_with_one_compact_patch() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[84; 32], 8 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "command_id": "branch-parent-append",
                    "actor": "human",
                    "projection": {"content": "parent"},
                    "turn_id": "branch-parent",
                    "conversation_defaults": {
                        "allowCreate": true,
                        "title": "Branches",
                        "settings": {}
                    }
                }),
                Some("branch-parent-append"),
            ),
            1_000,
        )
        .unwrap();
        let created = execute(
            &mut database,
            &request(
                "turn.branch.create",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "parent_turn_id": "branch-parent",
                    "expected_projection_revision": 1,
                    "title": "Alternative",
                    "kind": "comparison",
                    "anchor_text": "anchor",
                    "parent_selection": "selected"
                }),
                Some("create-outer-branch"),
            ),
            2_000,
        )
        .unwrap();
        let lane_id = created["lane"]["laneId"].as_str().unwrap().to_owned();
        assert!(lane_id.starts_with("lane_"));
        assert_eq!(created["conversationRevision"], 2);
        assert_eq!(created["turn"]["projectionRevision"], 2);
        assert_eq!(created["lane"]["parentTurnId"], "branch-parent");
        assert_eq!(created["lane"]["icon"], "⑂");

        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "command_id": "branch-child-append",
                    "actor": "assistant",
                    "lane_id": lane_id,
                    "projection": {"content": "child"},
                    "turn_id": "branch-child"
                }),
                Some("branch-child-append"),
            ),
            3_000,
        )
        .unwrap();
        let nested = execute(
            &mut database,
            &request(
                "turn.branch.create",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "parent_turn_id": "branch-child",
                    "expected_projection_revision": 1
                }),
                Some("create-nested-branch"),
            ),
            4_000,
        )
        .unwrap();
        let nested_lane_id = nested["lane"]["laneId"].as_str().unwrap().to_owned();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "command_id": "nested-child-append",
                    "actor": "assistant",
                    "lane_id": nested_lane_id,
                    "projection": {"content": "nested"},
                    "turn_id": "nested-child"
                }),
                Some("nested-child-append"),
            ),
            5_000,
        )
        .unwrap();
        let deleted = execute(
            &mut database,
            &request(
                "turn.branch.delete",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "parent_turn_id": "branch-parent",
                    "lane_id": lane_id
                }),
                Some("delete-outer-branch"),
            ),
            6_000,
        )
        .unwrap();
        assert_eq!(deleted["deletedLaneId"], lane_id);
        assert_eq!(
            deleted["deletedTurnIds"],
            json!(["branch-child", "nested-child"])
        );
        assert_eq!(deleted["conversationRevision"], 6);
        assert_eq!(deleted["turn"]["projectionRevision"], 3);
        assert_eq!(deleted["turn"]["projection"]["_branchLanes"], json!([]));

        let conversation = execute(
            &mut database,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "branch-conversation", "user_id": 11}),
                None,
            ),
            6_001,
        )
        .unwrap();
        assert_eq!(conversation["metadata"]["rev"], 6);
        assert_eq!(conversation["metadata"]["msg_count"], 1);
        assert_eq!(conversation["messages"].as_array().unwrap().len(), 1);
        assert_eq!(conversation["messages"][0]["content"], "parent");
        assert_eq!(conversation["messages"][0]["_projectionRevision"], 3);
        assert_eq!(conversation["messages"][0]["_branchLanes"], json!([]));
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "after": 5
                }),
                None,
            ),
            6_002,
        )
        .unwrap();
        assert_eq!(changes["head"], 6);
        assert_eq!(changes["events"].as_array().unwrap().len(), 1);
        assert_eq!(changes["events"][0]["type"], "turn.patch");
        assert_eq!(
            changes["events"][0]["payload"]["deletedTurnIds"],
            json!(["branch-child", "nested-child"])
        );
        assert_eq!(
            changes["events"][0]["payload"]["turnPatches"][0]["targetProjectionRevision"],
            3
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            6
        );
        let empty = execute(
            &mut database,
            &request(
                "turn.branch.create",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "parent_turn_id": "branch-parent",
                    "expected_projection_revision": 3
                }),
                Some("create-empty-branch"),
            ),
            7_000,
        )
        .unwrap();
        let empty_lane_id = empty["lane"]["laneId"].as_str().unwrap();
        let empty_deleted = execute(
            &mut database,
            &request(
                "turn.branch.delete",
                11,
                json!({
                    "conversation_id": "branch-conversation",
                    "user_id": 11,
                    "parent_turn_id": "branch-parent",
                    "lane_id": empty_lane_id
                }),
                Some("delete-empty-branch"),
            ),
            8_000,
        )
        .unwrap();
        assert_eq!(empty_deleted["deletedTurnIds"], json!([]));
        assert_eq!(empty_deleted["conversationRevision"], 8);
        assert_eq!(empty_deleted["turn"]["projectionRevision"], 5);
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            8
        );
    }

    #[test]
    fn tenant_global_turn_claim_serializes_concurrent_cross_owner_appends() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (owner_user_id, conversation_id) in [(11, "owner-a"), (12, "owner-b")] {
            let mut transaction = database
                .begin_with_identity_claim_scopes(7, owner_user_id)
                .unwrap();
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &crate::conversation_header::CreateRequest {
                    conversation_id: conversation_id.to_owned(),
                    title: conversation_id.to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1,
                    updated_at_ms: 1,
                    committed_at_ms: 1,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }

        let append_request = |conversation_id: &str| crate::turn::AppendSettledRequest {
            conversation_id: conversation_id.to_owned(),
            actor: "human".to_owned(),
            status: "completed".to_owned(),
            projection_json: b"{}".to_vec(),
            projection_timestamp: None,
                        settlement_json: b"{}".to_vec(),
            lane_id: "main".to_owned(),
            command_id: format!("append-{conversation_id}"),
            kind: "fixture".to_owned(),
            run_id: String::new(),
            turn_id: "tenant-global-turn".to_owned(),
            attempt_id: None,
            created_at_ms: 2,
            committed_at_ms: 2,
            defaults: crate::conversation_header::TurnDefaults {
                allow_create: false,
                title: "unused".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 2,
            },
        };
        let mut owner_a = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let mut owner_b = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        crate::turn::append_settled(&database, &mut owner_a, &append_request("owner-a")).unwrap();
        crate::turn::append_settled(&database, &mut owner_b, &append_request("owner-b")).unwrap();
        database.commit(owner_a).unwrap();
        assert_eq!(
            database.commit(owner_b).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );

        let owner_a_list = request(
            "turn.list",
            11,
            json!({"conversation_id": "owner-a", "user_id": 11}),
            None,
        );
        let owner_b_list = request(
            "turn.list",
            12,
            json!({"conversation_id": "owner-b", "user_id": 12}),
            None,
        );
        assert_eq!(
            execute(&mut database, &owner_a_list, 3)
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(execute(&mut database, &owner_b_list, 3)
            .unwrap()
            .as_array()
            .unwrap()
            .is_empty());
    }

    #[test]
    fn conversation_list_crosses_range_pages_without_n_plus_one_point_reads() {
        let vfs = prepared_vfs();
        let data_dir = std::path::Path::new("/data");
        let mut database = AuthorityDatabase::initialize_with_vfs(data_dir, vfs.clone()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in 0..=1_000 {
            let conversation_id = format!("conversation-{index:04}");
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &crate::conversation_header::CreateRequest {
                    conversation_id: conversation_id.clone(),
                    title: conversation_id,
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                    updated_at_ms: 1_000,
                    committed_at_ms: 1_000,
                },
            )
            .unwrap();
        }
        database.commit(transaction).unwrap();
        database.checkpoint_for_test().unwrap();
        drop(database);
        vfs.crash().unwrap();

        let mut database = AuthorityDatabase::open_with_vfs(data_dir, vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        let small_list = request(
            "conversation.list",
            11,
            json!({"user_id": 11, "include_messages": false, "limit": 50}),
            None,
        );
        let small_documents = execute(&mut database, &small_list, 2_000).unwrap();
        let small_reads = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        drop(database);
        let mut database = AuthorityDatabase::open_with_vfs(data_dir, vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();

        let list = request(
            "conversation.list",
            11,
            json!({"user_id": 11, "include_messages": false, "limit": 1_001}),
            None,
        );
        let documents = execute(&mut database, &list, 2_000).unwrap();
        let full_reads = vfs
            .trace()
            .unwrap()
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        let documents = documents.as_array().unwrap();
        assert_eq!(documents.len(), 1_001);
        assert_eq!(documents[0]["metadata"]["id"], "conversation-1000");
        assert_eq!(documents[1_000]["metadata"]["id"], "conversation-0000");
        assert_eq!(small_documents.as_array().unwrap(), &documents[..50]);
        assert!(
            small_reads * 4 < full_reads,
            "a cold 50-row page must avoid reading the 1,001-row catalog: small={small_reads}, full={full_reads}"
        );

        drop(database);
        let mut database = AuthorityDatabase::open_with_vfs(data_dir, vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        let catalog = request(
            "conversation.list",
            11,
            json!({"user_id": 11, "catalog_page": true, "limit": 50}),
            None,
        );
        let page = execute(&mut database, &catalog, 2_000).unwrap();
        let catalog_trace = vfs.trace().unwrap();
        let catalog_reads = catalog_trace
            .iter()
            .filter(|operation| **operation == Operation::Read)
            .count();
        assert_eq!(page["items"].as_array().unwrap(), &documents[..50]);
        assert_eq!(page["total_count"], 1_001);
        assert_eq!(page["has_more"], true);
        assert!(!catalog_trace.contains(&Operation::Write));
        assert!(
            catalog_reads * 4 < full_reads,
            "a cold catalog page must read only its results and has_more witness: catalog={catalog_reads}, full={full_reads}"
        );
        eprintln!(
            "cold conversation reads: list50={small_reads}, catalog50={catalog_reads}, list1001={full_reads}"
        );
        let following_page = request(
            "conversation.list",
            11,
            json!({"user_id": 11, "catalog_page": true, "limit": 1_000,
                "before_updated_at": 1_000, "before_id": "conversation-0951"}),
            None,
        );
        let remaining = execute(&mut database, &following_page, 2_000).unwrap();
        assert_eq!(remaining["items"].as_array().unwrap(), &documents[50..]);
        assert_eq!(remaining["total_count"], 1_001);
        assert_eq!(remaining["has_more"], false);

        let filtered_list = request(
            "conversation.list",
            11,
            json!({"user_id": 11, "include_messages": false, "limit": 1,
                "title_contains": "conversation-0000"}),
            None,
        );
        let filtered = execute(&mut database, &filtered_list, 2_000).unwrap();
        assert_eq!(filtered.as_array().unwrap(), &documents[1_000..]);
    }

    #[test]
    fn record_domain_matches_version_cas_prefix_order_owner_scope_and_blob_reopen() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            database
                .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
                .unwrap();
            let missing = request(
                "record.get",
                11,
                json!({"namespace": "contract", "key": "a"}),
                None,
            );
            assert_eq!(
                execute(&mut database, &missing, 1_000).unwrap(),
                Value::Null
            );

            let large_value = "值".repeat(10_000);
            let put_a = request(
                "record.put",
                11,
                json!({"namespace": "contract", "key": "a", "value": large_value.clone()}),
                Some("record-put-a"),
            );
            let first = execute(&mut database, &put_a, 2_000).unwrap();
            assert_eq!(
                first,
                json!({"key": "a", "version": 1, "updated_at_ms": 2000})
            );
            assert_eq!(execute(&mut database, &put_a, 9_000).unwrap(), first);

            for (key, command) in [("aa", "record-put-aa"), ("b", "record-put-b")] {
                let put = request(
                    "record.put",
                    11,
                    json!({"namespace": "contract", "key": key, "value": key}),
                    Some(command),
                );
                execute(&mut database, &put, 3_000).unwrap();
            }
            let list = request(
                "record.list",
                11,
                json!({"namespace": "contract", "prefix": "a", "limit": 10}),
                None,
            );
            let listed = execute(&mut database, &list, 4_000).unwrap();
            assert_eq!(listed.as_array().unwrap().len(), 2);
            assert_eq!(listed[0]["key"], "a");
            assert_eq!(listed[1]["key"], "aa");
            assert_eq!(listed[0]["value"], large_value);

            let foreign_get = request(
                "record.get",
                12,
                json!({"namespace": "contract", "key": "a"}),
                None,
            );
            assert_eq!(
                execute(&mut database, &foreign_get, 4_100).unwrap(),
                Value::Null
            );

            let stale = request(
                "record.put",
                11,
                json!({"namespace": "contract", "key": "a", "value": 2, "expected_version": 0}),
                Some("record-stale"),
            );
            assert_eq!(
                execute(&mut database, &stale, 5_000).unwrap_err(),
                SemanticExecutionError::Conflict
            );
            let delete = request(
                "record.delete",
                11,
                json!({"namespace": "contract", "key": "aa", "expected_version": 1}),
                Some("record-delete-aa"),
            );
            assert_eq!(
                execute(&mut database, &delete, 6_000).unwrap(),
                json!({"deleted": true})
            );
            assert_eq!(
                execute(&mut database, &delete, 7_000).unwrap(),
                json!({"deleted": true})
            );
            assert_eq!(
                database.logical_outbox_status(7, 11).unwrap().last_sequence,
                4
            );
        }
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
            .unwrap();
        let get_a = request(
            "record.get",
            11,
            json!({"namespace": "contract", "key": "a"}),
            None,
        );
        assert_eq!(
            execute(&mut reopened, &get_a, 8_000).unwrap()["value"]
                .as_str()
                .unwrap()
                .chars()
                .count(),
            10_000
        );
        let unsupported = request(
            "record.list",
            11,
            json!({"namespace": "task_results", "key": "task"}),
            None,
        );
        assert_eq!(
            execute(&mut reopened, &unsupported, 8_100).unwrap_err(),
            SemanticExecutionError::UnsupportedOperation
        );
    }

    #[test]
    fn task_result_record_witness_is_owned_blob_bounded_and_survives_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let value = json!({"user_id":11,"task_id":"witness-task","status":"running",
            "content":"x".repeat(256 * 1024), "metadata":{"source":"preserve"}});
        let query = |owner, explicit_owner| request("record.get", owner,
            json!({"namespace":"task_results","key":"witness-task","user_id":explicit_owner}), None);
        assert_eq!(execute(&mut database, &query(11, 11), 1000).unwrap(), Value::Null);
        execute(&mut database, &request("task_results.checkpoint", 11,
            json!({"key":"witness-task","value":value,"expected_version":0}),
            Some("witness-checkpoint")), 2000).unwrap();
        let expected = json!({"value":value,"version":1,"updated_at_ms":2000});
        assert_eq!(execute(&mut database, &query(11, 11), 2001).unwrap(), expected);
        assert_eq!(execute(&mut database, &query(12, 12), 2002).unwrap(), Value::Null);
        assert_eq!(execute(&mut database, &query(11, 12), 2003).unwrap(), Value::Null);
        execute(&mut database, &request("task_results.abort", 11,
            json!({"task_id":"witness-task","user_id":11,"source":"user"}),
            Some("witness-abort")), 2100).unwrap();
        let mut after_abort = expected.clone();
        after_abort["version"] = json!(2);
        after_abort["updated_at_ms"] = json!(2100);
        after_abort["value"]["abort_requested_at"] = json!(2100);
        after_abort["value"]["abort_source"] = json!("user");
        assert_eq!(execute(&mut database, &query(11, 11), 2200).unwrap(), after_abort);
        execute(&mut database, &request("task_results.recover_running", 11,
            json!({"interrupted_reason":"server_restart","max_rows":1,"scan_limit":10}),
            Some("witness-recover")), 3000).unwrap();
        let mut recovered = after_abort;
        recovered["version"] = json!(3);
        recovered["updated_at_ms"] = json!(3000);
        recovered["value"]["status"] = json!("interrupted");
        recovered["value"]["completed_at"] = json!(3000);
        recovered["value"]["interruptedReason"] = json!("server_restart");
        assert_eq!(execute(&mut database, &query(11, 11), 3001).unwrap(), recovered);
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(execute(&mut reopened, &query(11, 11), 3100).unwrap(), recovered);
    }

    #[test]
    fn task_result_checkpoint_is_blob_bounded_owner_scoped_and_replay_safe() {
        let directory = tempfile::tempdir().unwrap();
        let content = "x".repeat(9 * 1024 * 1024);
        let first = request(
            "task_results.checkpoint",
            11,
            json!({
                "key": "task-large",
                "value": {
                    "user_id": 11,
                    "task_id": "task-large",
                    "status": "pending",
                    "content": content,
                },
                "expected_version": 0,
            }),
            Some("task-checkpoint-first"),
        );
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            database
                .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
                .unwrap();
            let committed = execute(&mut database, &first, 2_000).unwrap();
            assert_eq!(
                committed,
                json!({"key": "task-large", "version": 1, "updated_at_ms": 2000})
            );
            let replay = request(
                "task_results.checkpoint",
                11,
                serde_json::from_slice(&first.payload).unwrap(),
                Some("task-checkpoint-lost-ack"),
            );
            assert_eq!(execute(&mut database, &replay, 3_000).unwrap(), committed);

            let mut changed: Value = serde_json::from_slice(&first.payload).unwrap();
            changed["value"]["status"] = json!("running");
            let stale = request(
                "task_results.checkpoint",
                11,
                changed,
                Some("task-checkpoint-stale"),
            );
            assert_eq!(
                execute(&mut database, &stale, 4_000).unwrap_err(),
                SemanticExecutionError::Conflict
            );
            let foreign = request(
                "task_results.checkpoint",
                12,
                serde_json::from_slice(&first.payload).unwrap(),
                Some("task-checkpoint-foreign"),
            );
            assert_eq!(
                execute(&mut database, &foreign, 4_100).unwrap_err(),
                SemanticExecutionError::AuthorityScopeMismatch
            );
        }
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(execute(&mut reopened, &first, 5_000).unwrap()["version"], 1);
    }

    #[test]
    fn task_result_recovery_pending_tracks_live_task_and_attempt_state() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let probe = request("task_results.recovery_pending", 11, json!({}), None);
        assert_eq!(
            execute(&mut database, &probe, 1_000).unwrap(),
            json!({"pending": false})
        );

        let checkpoint = request(
            "task_results.checkpoint",
            11,
            json!({
                "key": "task-live",
                "value": {
                    "user_id": 11,
                    "task_id": "task-live",
                    "status": "running",
                    "content": "payload",
                },
                "expected_version": 0,
            }),
            Some("task-live-checkpoint"),
        );
        execute(&mut database, &checkpoint, 2_000).unwrap();
        assert_eq!(
            execute(&mut database, &probe, 2_100).unwrap(),
            json!({"pending": true})
        );

        let recover = request(
            "task_results.recover_running",
            11,
            json!({"interrupted_reason": "server_restart", "max_rows": 32,
                   "scan_limit": 10_000}),
            Some("task-live-recover"),
        );
        execute(&mut database, &recover, 3_000).unwrap();
        assert_eq!(
            execute(&mut database, &probe, 3_100).unwrap(),
            json!({"pending": false})
        );
    }

    #[test]
    fn task_result_replay_summary_and_abort_never_rewrite_the_payload_document() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let checkpoint = request(
            "task_results.checkpoint",
            11,
            json!({
                "key": "task-compact",
                "value": {
                    "user_id": 11,
                    "task_id": "task-compact",
                    "conv_id": "conversation-compact",
                    "status": "running",
                    "content": "x".repeat(20_000),
                    "thinking": "private",
                    "metadata": {"model": "test-model", "rounds": 2},
                    "created_at": 100,
                },
                "expected_version": 0,
            }),
            Some("task-compact-checkpoint"),
        );
        execute(&mut database, &checkpoint, 2_000).unwrap();
        let payload_key = EntityKey::new(
            7,
            crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            crate::generated_tofudb_ir::TASK_RESULT_DOCUMENT_NAMESPACE,
            b"task-compact",
        )
        .unwrap();
        let payload_before = {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            database.entity_get(&mut transaction, &payload_key).unwrap()
        };

        let replay = request(
            "task_results.replay_get",
            11,
            json!({"key": "task-compact", "user_id": 11}),
            None,
        );
        let compact = execute(&mut database, &replay, 2_100).unwrap();
        assert_eq!(compact["metadata"], json!({"model": "test-model"}));
        assert!(compact.get("content").is_none());
        let summary = request(
            "task_results.summary_list",
            11,
            json!({"user_id": 11, "conv_id": "conversation-compact"}),
            None,
        );
        assert_eq!(
            execute(&mut database, &summary, 2_200).unwrap()["scanned"],
            1
        );
        let abort = request(
            "task_results.abort",
            11,
            json!({"task_id": "task-compact", "user_id": 11, "source": "test"}),
            Some("task-compact-abort"),
        );
        assert_eq!(execute(&mut database, &abort, 2_300).unwrap()["version"], 2);
        let payload_after = {
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            database.entity_get(&mut transaction, &payload_key).unwrap()
        };
        assert_eq!(payload_after, payload_before);

        let requested = request(
            "task_results.abort_requested",
            11,
            json!({"task_id": "task-compact", "user_id": 11}),
            None,
        );
        assert_eq!(
            execute(&mut database, &requested, 2_400).unwrap(),
            json!({"requested": true})
        );
        assert_eq!(
            execute(&mut database, &checkpoint, 2_500).unwrap_err(),
            SemanticExecutionError::Conflict
        );
    }

    #[test]
    fn record_pages_are_byte_bounded_and_corrupt_envelopes_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..10 {
            let put = request(
                "record.put",
                11,
                json!({
                    "namespace": "bounded",
                    "key": format!("row-{index:02}"),
                    "value": "x".repeat(900_000),
                }),
                Some(&format!("bounded-put-{index}")),
            );
            execute(&mut database, &put, 1_000 + index).unwrap();
        }
        let list = request(
            "record.list",
            11,
            json!({"namespace": "bounded", "limit": 10}),
            None,
        );
        assert_eq!(
            execute(&mut database, &list, 2_000).unwrap_err(),
            SemanticExecutionError::ResourceExhausted
        );

        let corrupt_key = record_key(7, 11, "corrupt", "row").unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut transaction, corrupt_key, b"not-a-document".to_vec())
            .unwrap();
        database.commit(transaction).unwrap();
        let get = request(
            "record.get",
            11,
            json!({"namespace": "corrupt", "key": "row"}),
            None,
        );
        assert!(matches!(
            execute(&mut database, &get, 2_100).unwrap_err(),
            SemanticExecutionError::Integrity(_)
        ));
    }

    #[test]
    fn event_domain_preserves_sparse_natural_keys_bounds_order_and_owner_scope() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[73; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        for sequence in [9_u64, 3, 12] {
            let append = request(
                "event.append",
                11,
                json!({"task_id": "sparse", "sequence": sequence, "event": {"type": "progress", "sequence": sequence}}),
                Some(&format!("event-{sequence}")),
            );
            assert_eq!(
                execute(&mut database, &append, 1_000 + sequence).unwrap()["inserted"],
                true
            );
        }
        let replay = request(
            "event.append",
            11,
            json!({"task_id": "sparse", "sequence": 9, "event": {"type": "progress", "sequence": 9}}),
            Some("event-9-replay"),
        );
        assert_eq!(
            execute(&mut database, &replay, 2_000).unwrap()["inserted"],
            false
        );
        let conflict = request(
            "event.append",
            11,
            json!({"task_id": "sparse", "sequence": 9, "event": {"type": "different"}}),
            Some("event-9-conflict"),
        );
        assert_eq!(
            execute(&mut database, &conflict, 2_100).unwrap_err(),
            SemanticExecutionError::Conflict
        );

        let bounds = request("event.bounds", 11, json!({"task_id": "sparse"}), None);
        assert_eq!(
            execute(&mut database, &bounds, 2_200).unwrap(),
            json!({"retained_count": 3, "base_cursor": 3, "next_cursor": 13})
        );
        let list = request(
            "event.list",
            11,
            json!({"task_id": "sparse", "after_sequence": 3, "limit": 10}),
            None,
        );
        let listed = execute(&mut database, &list, 2_300).unwrap();
        assert_eq!(
            listed
                .as_array()
                .unwrap()
                .iter()
                .map(|row| row["sequence"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![9, 12]
        );
        let latest = request("event.latest", 11, json!({"task_id": "sparse"}), None);
        assert_eq!(
            execute(&mut database, &latest, 2_400).unwrap()["sequence"],
            12
        );
        let foreign = request("event.bounds", 12, json!({"task_id": "sparse"}), None);
        assert_eq!(
            execute(&mut database, &foreign, 2_500).unwrap(),
            json!({"retained_count": 0, "base_cursor": 0, "next_cursor": 0})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            3
        );
    }

    #[test]
    fn event_execution_clock_survives_delayed_commit_and_replay() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.configure_logical_outbox(&[74; 32], 4 * 1024 * 1024, 512 * 1024).unwrap();
        let event = json!({"type": "delta", "content": "synthetic token",
            "executionAtMs": 1123, "executionOriginAtMs": 1000,
            "executionClockId": "producer-clock"});
        let append = request("event.append", 11,
            json!({"task_id": "event-clock", "sequence": 0, "event": event}),
            Some("event-clock-append"));
        execute(&mut database, &append, 9000).unwrap();
        let read = request("event.list", 11,
            json!({"task_id": "event-clock", "payload_types": []}), None);
        let rows = execute(&mut database, &read, 10000).unwrap();
        assert_eq!(rows[0]["created_at_ms"], 1123);
        assert_eq!(rows[0]["event"], json!({"type": "delta"}));
        let mut delta_spine = event.clone();
        delta_spine.as_object_mut().unwrap().remove("executionOriginAtMs");
        delta_spine.as_object_mut().unwrap().remove("executionClockId");
        let payload = json!({"task_id": "delta-clock", "sequence": 0, "event": delta_spine});
        assert_eq!(compile_event_item(payload.as_object().unwrap(), 9000).unwrap().created_at_ms, 1123);
        for invalid in [Value::Bool(true), Value::Null, json!(-1), json!(1.5)] {
            let mut malformed = event.clone();
            malformed["executionAtMs"] = invalid;
            let payload = json!({"task_id": "invalid-clock", "sequence": 0, "event": malformed});
            assert!(compile_event_item(payload.as_object().unwrap(), 9000).is_err());
        }
    }

    #[test]
    fn event_time_spine_selects_payloads_without_changing_replay_or_owner_scope() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.configure_logical_outbox(&[74; 32], 4 * 1024 * 1024, 512 * 1024).unwrap();
        let events = vec![
            json!({"type": "delta", "content": "x".repeat(2048)}),
            json!({"type": "round_start", "roundNum": 2}),
            json!({"type": "delta", "content": "y".repeat(2048)}),
        ];
        for (index, event) in events.iter().enumerate() {
            let append = request("event.append", 11,
                json!({"task_id": "time-spine", "sequence": index, "event": event}),
                Some(&format!("time-spine-{index}")));
            execute(&mut database, &append, 1000 + index as u64).unwrap();
        }
        let full_request = request("event.list", 11, json!({"task_id": "time-spine"}), None);
        let full = execute(&mut database, &full_request, 9000).unwrap();
        let selected_request = request("event.list", 11,
            json!({"task_id": "time-spine", "payload_types": ["round_start"]}), None);
        let selected = execute(&mut database, &selected_request, 9000).unwrap();
        assert_eq!(selected[0]["event"], json!({"type": "delta"}));
        assert_eq!(selected[1], full[1]);
        assert_eq!(selected[2]["event"], json!({"type": "delta"}));
        for index in 0..3 {
            assert_eq!(selected[index]["sequence"], full[index]["sequence"]);
            assert_eq!(selected[index]["created_at_ms"], full[index]["created_at_ms"]);
        }
        let null_selection = request("event.list", 11,
            json!({"task_id": "time-spine", "payload_types": null}), None);
        assert_eq!(execute(&mut database, &null_selection, 9000).unwrap(), full);
        let filtered = request("event.list", 11,
            json!({"task_id": "time-spine", "types": ["delta"],
                "after_sequence": 0, "limit": 1, "payload_types": []}), None);
        assert_eq!(execute(&mut database, &filtered, 9000).unwrap(),
            json!([{"sequence": 2, "created_at_ms": 1002, "event": {"type": "delta"}}]));
        let latest = request("event.latest", 11,
            json!({"task_id": "time-spine", "metadata_only": true}), None);
        assert_eq!(execute(&mut database, &latest, 9000).unwrap(),
            json!({"sequence": 2, "created_at_ms": 1002, "event": {"type": "delta"}}));
        let latest_full = request("event.latest", 11, json!({"task_id": "time-spine"}), None);
        assert_eq!(execute(&mut database, &latest_full, 9000).unwrap(), full[2]);
        let other_owner = request("event.list", 12,
            json!({"task_id": "time-spine", "payload_types": []}), None);
        assert_eq!(execute(&mut database, &other_owner, 9000).unwrap(), json!([]));
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(execute(&mut database, &selected_request, 9000).unwrap(), selected);
    }

    #[test]
    fn event_time_spine_rejects_unbounded_or_malformed_selection() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for selection in [json!(true), json!("delta"), json!([null]), json!([""]),
                          json!(["x".repeat(129)]), json!(vec!["delta"; 65])] {
            let req = request("event.list", 11,
                json!({"task_id": "time-spine", "payload_types": selection}), None);
            assert_eq!(execute(&mut database, &req, 1000).unwrap_err(),
                SemanticExecutionError::InvalidPayload);
        }
        for selection in [Value::Null, json!("true"), json!(1)] {
            let req = request("event.latest", 11,
                json!({"task_id": "time-spine", "metadata_only": selection}), None);
            assert_eq!(execute(&mut database, &req, 1000).unwrap_err(),
                SemanticExecutionError::InvalidPayload);
        }
    }

    #[test]
    fn event_time_spine_read_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.configure_logical_outbox(&[74; 32], 4 * 1024 * 1024, 512 * 1024).unwrap();
        let events = (0..200).map(|index| json!({
            "task_id": "spine-budget", "sequence": index,
            "event": {"type": "delta", "content": "x".repeat(2048)},
        })).collect::<Vec<_>>();
        execute(&mut database, &request("event.append_batch", 11,
            json!({"events": events}), Some("spine-budget-batch")), 1000).unwrap();
        let full = request("event.list", 11,
            json!({"task_id": "spine-budget", "limit": 200}), None);
        let spine = request("event.list", 11,
            json!({"task_id": "spine-budget", "limit": 200, "payload_types": []}), None);
        let mut elapsed = [Vec::new(), Vec::new()];
        let mut lengths = [0; 2];
        for _ in 0..5 {
            for (lane, req) in [&full, &spine].into_iter().enumerate() {
                let start = std::time::Instant::now();
                let rows = execute(&mut database, req, 9000).unwrap();
                elapsed[lane].push(start.elapsed().as_micros());
                assert_eq!(rows.as_array().unwrap().len(), 200);
                lengths[lane] = serde_json::to_vec(&rows).unwrap().len();
            }
        }
        elapsed.iter_mut().for_each(|values| values.sort_unstable());
        assert!(lengths[1] * 10 < lengths[0]);
        eprintln!("event_time_spine_budget rows=200 token_bytes=2048 full_bytes={} spine_bytes={} full_median_us={} spine_median_us={}",
            lengths[0], lengths[1], elapsed[0][2], elapsed[1][2]);
    }

    #[test]
    fn event_batch_is_atomic_filtered_and_supports_five_hundred_streams() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[74; 32], 4 * 1024 * 1024, 512 * 1024)
            .unwrap();

        let events = (0..100)
            .map(|index| {
                json!({
                    "task_id": format!("batch-task-{}", index % 2),
                    "sequence": index / 2,
                    "event": {
                        "type": if index % 4 < 2 { "progress" } else { "terminal.done" },
                        "index": index,
                    },
                })
            })
            .collect::<Vec<_>>();
        let append = request(
            "event.append_batch",
            11,
            json!({"events": events.clone()}),
            Some("event-batch-1"),
        );
        let appended = execute(&mut database, &append, 2_000).unwrap();
        assert_eq!(appended["inserted"], 100);
        assert_eq!(appended["deduplicated"], 0);
        assert_eq!(appended["results"].as_array().unwrap().len(), 100);

        let replay = request(
            "event.append_batch",
            11,
            json!({"events": events}),
            Some("event-batch-replay"),
        );
        let replayed = execute(&mut database, &replay, 2_100).unwrap();
        assert_eq!(replayed["inserted"], 0);
        assert_eq!(replayed["deduplicated"], 100);
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            1
        );

        let exact_filter = request(
            "event.list",
            11,
            json!({"task_id": "batch-task-0", "types": ["progress"], "limit": 100}),
            None,
        );
        let exact_rows = execute(&mut database, &exact_filter, 2_110).unwrap();
        assert_eq!(exact_rows.as_array().unwrap().len(), 25);
        assert!(exact_rows
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["event"]["type"] == "progress"));
        let prefix_filter = request(
            "event.list",
            11,
            json!({
                "task_id": "batch-task-0",
                "types": ["progress"],
                "type_prefixes": ["terminal."],
                "after_sequence": 9,
                "limit": 10
            }),
            None,
        );
        let prefix_rows = execute(&mut database, &prefix_filter, 2_120).unwrap();
        assert_eq!(prefix_rows.as_array().unwrap().len(), 10);
        assert_eq!(prefix_rows[0]["sequence"], 10);
        let invalid_prefix = request(
            "event.list",
            11,
            json!({"task_id": "batch-task-0", "type_prefixes": ["tool*"]}),
            None,
        );
        assert_eq!(
            execute(&mut database, &invalid_prefix, 2_130).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );

        let maximum_events = (0..500)
            .map(|index| {
                json!({
                    "task_id": format!("maximum-task-{index}"),
                    "sequence": 0,
                    "event": {"type": format!("kind-{index}"), "index": index},
                })
            })
            .collect::<Vec<_>>();
        let maximum = request(
            "event.append_batch",
            11,
            json!({"events": maximum_events}),
            Some("event-batch-maximum"),
        );
        let maximum_result = execute(&mut database, &maximum, 2_150).unwrap();
        assert_eq!(maximum_result["inserted"], 500);
        assert_eq!(maximum_result["results"].as_array().unwrap().len(), 500);

        let wide_events = (0..200)
            .map(|index| {
                json!({
                    "task_id": format!("wide-task-{index}"),
                    "sequence": 0,
                    "event": {"type": "created", "index": index},
                })
            })
            .collect::<Vec<_>>();
        let wide = request(
            "event.append_batch",
            11,
            json!({"events": wide_events}),
            Some("event-batch-wide"),
        );
        let wide_result = execute(&mut database, &wide, 2_200).unwrap();
        assert_eq!(wide_result["inserted"], 200);
        assert_eq!(wide_result["results"].as_array().unwrap().len(), 200);
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            3
        );

        let excessive_items = (0..501)
            .map(|_| {
                json!({
                    "task_id": "excessive-task",
                    "sequence": 0,
                    "event": null,
                })
            })
            .collect::<Vec<_>>();
        let excessive = request(
            "event.append_batch",
            11,
            json!({"events": excessive_items}),
            Some("event-batch-excessive-items"),
        );
        assert_eq!(
            execute(&mut database, &excessive, 2_250).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );

        for batch in 0..=2 {
            let start = batch * 500;
            let end = (start + 500).min(1_001);
            if start == end {
                continue;
            }
            let scan_events = (start..end)
                .map(|sequence| {
                    json!({
                        "task_id": "filter-scan-budget",
                        "sequence": sequence,
                        "event": {
                            "type": if sequence == 1_000 { "target" } else { "noise" }
                        },
                    })
                })
                .collect::<Vec<_>>();
            let append_scan = request(
                "event.append_batch",
                11,
                json!({"events": scan_events}),
                Some(&format!("event-filter-scan-{batch}")),
            );
            execute(&mut database, &append_scan, 2_260 + batch).unwrap();
        }
        let indexed_beyond_primary_scan_budget = request(
            "event.list",
            11,
            json!({"task_id": "filter-scan-budget", "types": ["target"], "limit": 1}),
            None,
        );
        let indexed_result =
            execute(&mut database, &indexed_beyond_primary_scan_budget, 2_270).unwrap();
        assert_eq!(indexed_result.as_array().unwrap().len(), 1);
        assert_eq!(indexed_result[0]["sequence"], 1_000);

        for batch in 0..2 {
            let catalog_events = (batch * 500..(batch + 1) * 500)
                .map(|sequence| {
                    json!({
                        "task_id": "filter-catalog-budget",
                        "sequence": sequence,
                        "event": {"type": format!("catalog.{sequence:04}")},
                    })
                })
                .collect::<Vec<_>>();
            let append_catalog = request(
                "event.append_batch",
                11,
                json!({"events": catalog_events}),
                Some(&format!("event-filter-catalog-{batch}")),
            );
            execute(&mut database, &append_catalog, 2_280 + batch).unwrap();
        }
        let excessive_catalog = request(
            "event.list",
            11,
            json!({
                "task_id": "filter-catalog-budget",
                "type_prefixes": ["catalog."],
                "limit": 1
            }),
            None,
        );
        assert_eq!(
            execute(&mut database, &excessive_catalog, 2_290).unwrap_err(),
            SemanticExecutionError::ResourceExhausted
        );

        let conflicting = request(
            "event.append_batch",
            11,
            json!({"events": [
                {"task_id": "atomic-conflict", "sequence": 0, "event": {"value": "first"}},
                {"task_id": "atomic-conflict", "sequence": 0, "event": {"value": "second"}}
            ]}),
            Some("event-batch-conflict"),
        );
        assert_eq!(
            execute(&mut database, &conflicting, 2_300).unwrap_err(),
            SemanticExecutionError::Conflict
        );
        let bounds = request(
            "event.bounds",
            11,
            json!({"task_id": "atomic-conflict"}),
            None,
        );
        assert_eq!(
            execute(&mut database, &bounds, 2_400).unwrap(),
            json!({"retained_count": 0, "base_cursor": 0, "next_cursor": 0})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            8
        );
    }

    #[test]
    fn event_inspector_summary_folds_type_metadata_and_discovers_only_agent_children() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[75; 32], 1024 * 1024, 256 * 1024)
            .unwrap();
        let append = request(
            "event.append_batch",
            11,
            json!({"events": [
                {"task_id": "inspector-root", "sequence": 0, "event": {"type": "messages_snapshot", "kind": "request"}},
                {"task_id": "inspector-root", "sequence": 1, "event": {"type": "messages_snapshot", "kind": "state"}},
                {"task_id": "inspector-root", "sequence": 2, "event": {"type": "messages_snapshot"}},
                {"task_id": "inspector-root", "sequence": 3, "event": {"type": "delta"}},
                {"task_id": "inspector-root", "sequence": 4, "event": {"type": "flow_iteration"}},
                {"task_id": "inspector-root", "sequence": 5, "event": {"type": "endpoint_iteration"}},
                {"task_id": "inspector-root#agent:research", "sequence": 0, "event": {"type": "messages_snapshot", "kind": "request"}},
                {"task_id": "inspector-root#agent;escaped", "sequence": 0, "event": {"type": "flow_iteration"}}
            ]}),
            Some("inspector-events"),
        );
        execute(&mut database, &append, 5_000).unwrap();

        let summary = request(
            "event.inspector_summary",
            11,
            json!({"task_ids": ["inspector-root"]}),
            None,
        );
        assert_eq!(
            execute(&mut database, &summary, 5_100).unwrap(),
            json!({"records": [
                {
                    "task_id": "inspector-root",
                    "request_count": 1,
                    "state_count": 1,
                    "legacy_count": 1,
                    "event_count": 5,
                    "first_event_at_ms": 5_000
                },
                {
                    "task_id": "inspector-root#agent:research",
                    "request_count": 1,
                    "state_count": 0,
                    "legacy_count": 0,
                    "event_count": 1,
                    "first_event_at_ms": 5_000
                }
            ]})
        );
        let foreign = request(
            "event.inspector_summary",
            12,
            json!({"task_ids": ["inspector-root"]}),
            None,
        );
        assert_eq!(
            execute(&mut database, &foreign, 5_200).unwrap(),
            json!({"records": []})
        );
        let excessive = request(
            "event.inspector_summary",
            11,
            json!({"task_ids": (0..101).map(|index| format!("task-{index}")).collect::<Vec<_>>() }),
            None,
        );
        assert_eq!(
            execute(&mut database, &excessive, 5_300).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
    }

    #[test]
    fn event_prune_matches_retention_tiers_and_repairs_all_logical_indexes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[76; 32], 1024 * 1024, 256 * 1024)
            .unwrap();
        let cases = [
            (11, "retention-a", 10, json!({"type": "delta"}), 100),
            (
                11,
                "retention-a",
                2,
                json!({"type": "messages_snapshot", "kind": "request"}),
                200,
            ),
            (11, "retention-a", 30, json!({"opaque": true}), 300),
            (
                11,
                "retention-a",
                20,
                json!({"type": "messages_snapshot", "kind": "state"}),
                400,
            ),
            (11, "retention-a", 40, json!({"type": "delta"}), 500),
            (11, "retention-b", 7, json!({"type": "progress"}), 150),
            (12, "retention-a", 1, json!({"type": "delta"}), 50),
        ];
        for (owner, task_id, sequence, event, timestamp) in cases {
            let append = request(
                "event.append",
                owner,
                json!({"task_id": task_id, "sequence": sequence, "event": event}),
                Some(&format!("retention-{owner}-{task_id}-{sequence}")),
            );
            execute(&mut database, &append, timestamp).unwrap();
        }

        let streaming = request(
            "event.prune",
            11,
            json!({
                "created_before_ms": 400,
                "retention_class": "streaming",
                "limit": 2
            }),
            Some("retention-streaming-first"),
        );
        assert_eq!(
            execute(&mut database, &streaming, 1_000).unwrap(),
            json!({"deleted": 2, "has_more": true, "index_mode": "tier_partial_v2"})
        );
        let physical_a =
            crate::stream::StreamKey::new(7, 11, "task_event", b"retention-a").unwrap();
        assert_eq!(
            database
                .stream_read(7, 11, &physical_a, 1, 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            database
                .stream_read(7, 11, &physical_a, 2, 1)
                .unwrap()
                .events[0]
                .sequence,
            2
        );
        let list_a = request("event.list", 11, json!({"task_id": "retention-a"}), None);
        assert_eq!(
            execute(&mut database, &list_a, 1_001)
                .unwrap()
                .as_array()
                .unwrap()
                .iter()
                .map(|row| row["sequence"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![2, 20, 30, 40]
        );
        let bounds = request("event.bounds", 11, json!({"task_id": "retention-a"}), None);
        assert_eq!(
            execute(&mut database, &bounds, 1_002).unwrap(),
            json!({"retained_count": 4, "base_cursor": 2, "next_cursor": 41})
        );

        let structural = request(
            "event.prune",
            11,
            json!({
                "created_before_ms": 350,
                "retention_class": "structural",
                "limit": 1
            }),
            Some("retention-structural-first"),
        );
        assert_eq!(
            execute(&mut database, &structural, 1_010).unwrap()["deleted"],
            1
        );
        let summary = request(
            "event.inspector_summary",
            11,
            json!({"task_ids": ["retention-a"]}),
            None,
        );
        assert_eq!(
            execute(&mut database, &summary, 1_011).unwrap(),
            json!({"records": [{
                "task_id": "retention-a",
                "request_count": 0,
                "state_count": 1,
                "legacy_count": 0,
                "event_count": 1,
                "first_event_at_ms": 400
            }]})
        );
        let structural_blank = request(
            "event.prune",
            11,
            json!({"created_before_ms": 350, "retention_class": "structural"}),
            Some("retention-structural-blank"),
        );
        assert_eq!(
            execute(&mut database, &structural_blank, 1_020).unwrap()["deleted"],
            1
        );
        let final_streaming = request(
            "event.prune",
            11,
            json!({"created_before_ms": 1_000, "retention_class": "streaming"}),
            Some("retention-streaming-final"),
        );
        assert_eq!(
            execute(&mut database, &final_streaming, 1_030).unwrap()["deleted"],
            1
        );
        assert_eq!(
            execute(&mut database, &list_a, 1_031)
                .unwrap()
                .as_array()
                .unwrap()
                .iter()
                .map(|row| row["sequence"].as_u64().unwrap())
                .collect::<Vec<_>>(),
            vec![20]
        );
        let foreign = request("event.list", 12, json!({"task_id": "retention-a"}), None);
        assert_eq!(
            execute(&mut database, &foreign, 1_032).unwrap()[0]["sequence"],
            1
        );
        let zero_cutoff = request(
            "event.prune",
            11,
            json!({"created_before_ms": 0, "retention_class": "streaming"}),
            Some("zero-retention-cutoff"),
        );
        assert_eq!(
            execute(&mut database, &zero_cutoff, 1_035).unwrap(),
            json!({"deleted": 0, "has_more": false, "index_mode": "tier_partial_v2"})
        );
        let physical_b =
            crate::stream::StreamKey::new(7, 11, "task_event", b"retention-b").unwrap();
        assert_eq!(
            database
                .stream_read(7, 11, &physical_b, 1, 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        let append_after_retirement = request(
            "event.append",
            11,
            json!({"task_id": "retention-b", "sequence": 8, "event": {"type": "progress"}}),
            Some("retention-b-after-retirement"),
        );
        execute(&mut database, &append_after_retirement, 1_036).unwrap();
        assert_eq!(
            database
                .stream_read(7, 11, &physical_b, 2, 1)
                .unwrap()
                .events[0]
                .sequence,
            2
        );

        for payload in [
            json!({"created_before_ms": -1, "retention_class": "streaming"}),
            json!({"created_before_ms": 1, "retention_class": "unknown"}),
            json!({"created_before_ms": 1, "retention_class": "streaming", "limit": 1001}),
        ] {
            let invalid = request("event.prune", 11, payload, Some("invalid-retention"));
            assert_eq!(
                execute(&mut database, &invalid, 1_040).unwrap_err(),
                SemanticExecutionError::InvalidPayload
            );
        }
    }

    #[test]
    fn event_prune_admits_one_thousand_distinct_tasks_within_hard_write_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[77; 32], 4 * 1024 * 1024, 512 * 1024)
            .unwrap();
        for batch in 0..2 {
            let events = (batch * 500..(batch + 1) * 500)
                .map(|index| {
                    json!({
                        "task_id": format!("retention-maximum-{index:04}"),
                        "sequence": index * 2,
                        "event": {"type": "delta", "index": index}
                    })
                })
                .collect::<Vec<_>>();
            let append = request(
                "event.append_batch",
                11,
                json!({"events": events}),
                Some(&format!("retention-maximum-batch-{batch}")),
            );
            assert_eq!(
                execute(&mut database, &append, 1_000 + batch).unwrap()["inserted"],
                500
            );
        }
        let prune = request(
            "event.prune",
            11,
            json!({
                "created_before_ms": 2_000,
                "retention_class": "streaming",
                "limit": 1_000
            }),
            Some("retention-maximum-prune"),
        );
        assert_eq!(
            execute(&mut database, &prune, 2_000).unwrap(),
            json!({"deleted": 1_000, "has_more": true, "index_mode": "tier_partial_v2"})
        );
        for index in [0, 499, 999] {
            let bounds = request(
                "event.bounds",
                11,
                json!({"task_id": format!("retention-maximum-{index:04}")}),
                None,
            );
            assert_eq!(
                execute(&mut database, &bounds, 2_001).unwrap(),
                json!({"retained_count": 0, "base_cursor": 0, "next_cursor": 0})
            );
        }
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            3
        );
    }

    #[test]
    fn event_prune_resumes_a_stream_retirement_queue_beyond_one_segment_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[78; 32], 4 * 1024 * 1024, 512 * 1024)
            .unwrap();
        for index in 1..=130_u64 {
            let append = request(
                "event.append",
                11,
                json!({
                    "task_id": "retirement-resume",
                    "sequence": index,
                    "event": {"type": "delta", "index": index}
                }),
                Some(&format!("retirement-resume-{index}")),
            );
            execute(&mut database, &append, 1_000 + index).unwrap();
        }
        let prune = request(
            "event.prune",
            11,
            json!({
                "created_before_ms": 2_000,
                "retention_class": "streaming",
                "limit": 1_000
            }),
            Some("retirement-resume-prune"),
        );
        assert_eq!(
            execute(&mut database, &prune, 3_000).unwrap(),
            json!({"deleted": 130, "has_more": true, "index_mode": "tier_partial_v2"})
        );
        let resume = request(
            "event.prune",
            11,
            json!({"created_before_ms": 0, "retention_class": "streaming"}),
            Some("retirement-resume-drain"),
        );
        assert_eq!(
            execute(&mut database, &resume, 3_001).unwrap(),
            json!({"deleted": 0, "has_more": false, "index_mode": "tier_partial_v2"})
        );
        let physical =
            crate::stream::StreamKey::new(7, 11, "task_event", b"retirement-resume").unwrap();
        assert_eq!(
            database
                .stream_read(7, 11, &physical, 130, 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert!(
            database
                .stream_read(7, 11, &physical, 131, 1)
                .unwrap()
                .end_of_stream
        );
    }

    #[test]
    fn owner_payload_and_command_identity_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let wrong_owner = request(
            "desktop.egress_agent.get",
            11,
            json!({"owner_user_id": 12}),
            None,
        );
        assert_eq!(
            execute(&mut database, &wrong_owner, 1_000).unwrap_err(),
            SemanticExecutionError::AuthorityScopeMismatch
        );
        let oversized = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "x".repeat(129)}),
            Some("oversized-agent"),
        );
        assert_eq!(
            execute(&mut database, &oversized, 1_000).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
        let unicode_boundary = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "中".repeat(128)}),
            Some("unicode-boundary"),
        );
        assert_eq!(
            execute(&mut database, &unicode_boundary, 1_500).unwrap()["agent_id"],
            Value::String("中".repeat(128))
        );
        let unicode_oversized = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "中".repeat(129)}),
            Some("unicode-oversized"),
        );
        assert_eq!(
            execute(&mut database, &unicode_oversized, 1_600).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
        let first = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "agent-1"}),
            Some("same-command"),
        );
        execute(&mut database, &first, 2_000).unwrap();
        let reused = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "agent-2"}),
            Some("same-command"),
        );
        assert_eq!(
            execute(&mut database, &reused, 3_000).unwrap_err(),
            SemanticExecutionError::Conflict
        );
    }

    fn tool_result_put_request(owner_id: u64, content: &str, expires_at_ms: u64) -> Request {
        request(
            "tool_result_artifact.put",
            owner_id,
            json!({
                "user_id":owner_id, "content":content, "media_type":"text/plain",
                "created_at_ms":100, "expires_at_ms":expires_at_ms,
            }),
            Some(&format!("put-{owner_id}-{content}")),
        )
    }

    fn tool_result_prune_request(command_id: &str, limit: usize) -> Request {
        request(
            "tool_result_artifact.prune",
            11,
            json!({"now_ms":300,"limit":limit}),
            Some(command_id),
        )
    }

    #[test]
    fn tool_result_prune_is_owner_scoped_bounded_and_receiptless() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[74; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let expired_refs: Vec<String> = ["expired-a", "expired-b"]
            .into_iter()
            .map(|content| {
                execute(
                    &mut database,
                    &tool_result_put_request(11, content, 200),
                    1_000,
                )
                .unwrap()["artifactRef"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            })
            .collect();
        let future_ref = execute(
            &mut database,
            &tool_result_put_request(11, "future", 1_000),
            1_001,
        )
        .unwrap()["artifactRef"]
            .as_str()
            .unwrap()
            .to_owned();
        let foreign_ref = execute(
            &mut database,
            &tool_result_put_request(12, "foreign-expired", 200),
            1_002,
        )
        .unwrap()["artifactRef"]
            .as_str()
            .unwrap()
            .to_owned();

        let first = tool_result_prune_request("prune-first", 1);
        assert_eq!(
            execute(&mut database, &first, 1_003).unwrap(),
            json!({"deleted":1,"hasMore":true})
        );
        let remaining = tool_result_prune_request("prune-remaining", 5_000);
        assert_eq!(
            execute(&mut database, &remaining, 1_004).unwrap(),
            json!({"deleted":1,"hasMore":false})
        );
        assert_eq!(
            execute(&mut database, &remaining, 1_005).unwrap(),
            json!({"deleted":0,"hasMore":false})
        );
        for artifact_ref in expired_refs {
            assert!(execute(
                &mut database,
                &request(
                    "tool_result_artifact.read",
                    11,
                    json!({"user_id":11,"artifact_ref":artifact_ref,"now_ms":150}),
                    None,
                ),
                1_006,
            )
            .unwrap()
            .is_null());
        }
        assert!(!execute(
            &mut database,
            &request(
                "tool_result_artifact.read",
                11,
                json!({"user_id":11,"artifact_ref":future_ref,"now_ms":150}),
                None,
            ),
            1_007,
        )
        .unwrap()
        .is_null());
        assert!(!execute(
            &mut database,
            &request(
                "tool_result_artifact.read",
                12,
                json!({"user_id":12,"artifact_ref":foreign_ref,"now_ms":150}),
                None,
            ),
            1_008,
        )
        .unwrap()
        .is_null());
        assert_eq!(
            database
                .logical_outbox_pending(7, 11, 16)
                .unwrap()
                .iter()
                .filter(|record| {
                    record.record.identity.operation == "tool_result_artifact.prune"
                })
                .count(),
            2
        );
        for (payload, command_id) in [
            (json!({"now_ms":0}), "prune-zero-time"),
            (json!({"now_ms":300,"limit":5_001}), "prune-over-limit"),
        ] {
            assert_eq!(
                execute(
                    &mut database,
                    &request("tool_result_artifact.prune", 11, payload, Some(command_id),),
                    1_009,
                )
                .unwrap_err(),
                SemanticExecutionError::InvalidPayload
            );
        }
    }

    #[test]
    fn tool_result_prune_executes_five_thousand_rows_across_entity_pages() {
        let vfs = prepared_vfs();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        for chunk in 0..10 {
            let mut transaction = database.begin(7, 11).unwrap();
            for offset in 0..500 {
                crate::artifact::tool_put(
                    &database,
                    &mut transaction,
                    &format!("bulk-expired-{}", chunk * 500 + offset),
                    "text/plain",
                    100,
                    200,
                )
                .unwrap();
            }
            database.commit(transaction).unwrap();
        }
        let mut transaction = database.begin(7, 11).unwrap();
        crate::artifact::tool_put(
            &database,
            &mut transaction,
            "bulk-future",
            "text/plain",
            100,
            1_000,
        )
        .unwrap();
        database.commit(transaction).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();

        assert_eq!(
            execute(
                &mut database,
                &tool_result_prune_request("prune-five-thousand", 5_000),
                2_000,
            )
            .unwrap(),
            json!({"deleted":5_000,"hasMore":true})
        );
        assert_eq!(
            execute(
                &mut database,
                &tool_result_prune_request("prune-after-five-thousand", 5_000),
                2_001,
            )
            .unwrap(),
            json!({"deleted":0,"hasMore":false})
        );
    }

    fn prepared_tool_result_prune_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs);
        execute(
            &mut database,
            &tool_result_put_request(11, "fault-expired", 200),
            1_000,
        )
        .unwrap();
        database
    }

    fn assert_tool_result_prune_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 2_000).unwrap();
        assert!(
            replay == json!({"deleted":0,"hasMore":false})
                || replay == json!({"deleted":1,"hasMore":true})
        );
        let artifact_ref = format!("tool-result:{:x}", Sha256::digest(b"fault-expired"));
        assert!(execute(
            &mut recovered,
            &request(
                "tool_result_artifact.read",
                11,
                json!({"user_id":11,"artifact_ref":artifact_ref,"now_ms":150}),
                None,
            ),
            2_001,
        )
        .unwrap()
        .is_null());
        assert_eq!(
            recovered
                .logical_outbox_pending(7, 11, 16)
                .unwrap()
                .iter()
                .filter(|record| {
                    record.record.identity.operation == "tool_result_artifact.prune"
                })
                .count(),
            1
        );
    }

    #[test]
    fn every_tool_result_semantic_prune_fault_recovers_delete_index_and_outbox_atomically() {
        let command = tool_result_prune_request("fault-prune", 1);
        let baseline_vfs = prepared_vfs();
        let mut baseline = prepared_tool_result_prune_database(baseline_vfs.clone());
        baseline_vfs.arm_fault(None).unwrap();
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = prepared_tool_result_prune_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_tool_result_prune_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = prepared_tool_result_prune_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_tool_result_prune_recovers_atomic_prefix(vfs, &command);
        }
    }

    #[test]
    fn canonical_request_reuses_exact_bytes_for_owner_and_legacy_digests() {
        for payload in [
            json!({}),
            json!({"user_id": 11}),
            json!({"user_id": 11, "z": "last"}),
            json!({"a": "first", "user_id": 11}),
            json!({"a": {"escape": "\"\\\n中🙂", "float": 1.25},
                "user_id": 11, "z": [true, null, "tail"]}),
            json!({"content": "\"\\\n中🙂".repeat(32_000), "user_id": 11}),
        ] {
            let payload = payload.as_object().unwrap();
            let encoded = canonical_request("turn.append_settled", payload, 0);
            let reference = serde_json::to_vec(&json!({
                "operation": "turn.append_settled", "payload": payload,
            }))
            .unwrap();
            assert_eq!(encoded.bytes, reference);
            assert_eq!(encoded.digest, <[u8; 32]>::from(Sha256::digest(&reference)));
            let mut legacy = payload.clone();
            if legacy.remove("user_id").is_some() {
                let reference = serde_json::to_vec(&json!({
                    "operation": "turn.append_settled", "payload": legacy,
                }))
                .unwrap();
                assert_eq!(
                    encoded.ownerless_digest,
                    Some(Sha256::digest(reference).into())
                );
            } else {
                assert_eq!(encoded.ownerless_digest, None);
            }
        }
    }

    #[test]
    fn request_digest_matches_storage_v1_sorted_canonical_json() {
        let payload = payload_object(br#"{"owner_user_id":11,"agent_id":"agent-1"}"#).unwrap();
        let digest = request_digest("desktop.egress_agent.set", &payload);
        let hexadecimal = digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(
            hexadecimal,
            "15ea6d6b77231a8aa86e959934bd4225566fbd98a99b28ee4cd38ebb07983e49"
        );
    }

    #[test]
    fn migrated_ownerless_receipt_replays_past_injected_owner() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // A receipt exactly as the offline migration restores it: the v1
        // authority hashed the payload precisely as received, without the
        // storage.v2 session-injected owner.
        let legacy_digest = request_digest(
            "record.put",
            &payload_object(br#"{"namespace":"history","key":"k1","value":{"n":1}}"#).unwrap(),
        );
        let migrated_response = br#"{"version":4}"#;
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .receipt_insert(
                &mut transaction,
                "migrated-command-1",
                "record.put",
                legacy_digest,
                migrated_response,
                1_000,
            )
            .unwrap();
        database.commit(transaction).unwrap();

        // Live replay carries the injected owner; the migrated receipt must
        // still resolve instead of reporting command-ID reuse, and it must
        // not re-execute the mutation.
        let replay = execute(
            &mut database,
            &request(
                "record.put",
                11,
                json!({"user_id": 11, "namespace": "history", "key": "k1", "value": {"n": 1}}),
                Some("migrated-command-1"),
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(replay, json!({"version": 4}));
        let absent = execute(
            &mut database,
            &request(
                "record.get",
                11,
                json!({"user_id": 11, "namespace": "history", "key": "k1"}),
                None,
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(absent, Value::Null);

        // A genuinely different request under the same command ID conflicts.
        let conflict = execute(
            &mut database,
            &request(
                "record.put",
                11,
                json!({"user_id": 11, "namespace": "history", "key": "k1", "value": {"n": 2}}),
                Some("migrated-command-1"),
            ),
            2_000,
        )
        .unwrap_err();
        assert_eq!(conflict.code(), "database_conflict");
    }

    fn create_conversation(database: &mut AuthorityDatabase, conv_id: &str) {
        execute(
            database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": conv_id,
                    "user_id": 11,
                    "title": "t",
                    "settings": {},
                    "created_at": 1_000,
                    "updated_at": 1_000
                }),
                Some(&format!("conversation-create-{conv_id}")),
            ),
            1_000,
        )
        .unwrap();
    }

    fn checkpoint_task(
        database: &mut AuthorityDatabase,
        key: &str,
        value: Value,
        expected_version: u64,
        command_id: &str,
    ) -> Result<Value, SemanticExecutionError> {
        execute(
            database,
            &request(
                "task_results.checkpoint",
                11,
                json!({
                    "user_id": 11,
                    "key": key,
                    "value": value,
                    "expected_version": expected_version,
                }),
                Some(command_id),
            ),
            2_000,
        )
    }

    fn register_child(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
        parent_task_id: &str,
        child_task_id: &str,
        command_id: &str,
    ) -> Result<Value, SemanticExecutionError> {
        execute(
            database,
            &request(
                "invocation.register_child",
                11,
                json!({
                    "user_id": 11,
                    "conversation_id": conversation_id,
                    "parent_task_id": parent_task_id,
                    "child_task_id": child_task_id,
                }),
                Some(command_id),
            ),
            2_000,
        )
    }

    fn birth_identity(parent_task_id: &str) -> Value {
        json!({
            "user_id": 11,
            "conversation_id": "conv-1",
            "parent_task_id": parent_task_id,
            "child_task_id": "child-task",
            "archive_scope": "invocation:11:child-task",
        })
    }

    #[test]
    fn invocation_registration_resolves_and_fences_identity() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        create_conversation(&mut database, "conv-1");
        checkpoint_task(
            &mut database,
            "parent-task",
            json!({"task_id": "parent-task", "user_id": 11, "conv_id": "conv-1", "status": "running"}),
            0,
            "parent-birth",
        )
        .unwrap();

        let identity = register_child(
            &mut database,
            "conv-1",
            "parent-task",
            "child-task",
            "reg-1",
        )
        .unwrap();
        assert_eq!(identity, birth_identity("parent-task"));
        // Natural replay (no receipt on this op) returns the same identity.
        let replayed = register_child(
            &mut database,
            "conv-1",
            "parent-task",
            "child-task",
            "reg-2",
        )
        .unwrap();
        assert_eq!(replayed, identity);

        // Rebinding the immutable child identity conflicts.
        checkpoint_task(
            &mut database,
            "parent-task-2",
            json!({"task_id": "parent-task-2", "user_id": 11, "conv_id": "conv-1", "status": "running"}),
            0,
            "parent-birth-2",
        )
        .unwrap();
        let rebind = register_child(
            &mut database,
            "conv-1",
            "parent-task-2",
            "child-task",
            "reg-3",
        )
        .unwrap_err();
        assert_eq!(rebind.code(), "database_conflict");

        // Resolution authorizes the exact persisted identity only.
        let resolved = execute(
            &mut database,
            &request(
                "invocation.resolve_child",
                11,
                json!({"user_id": 11, "archive_scope": "invocation:11:child-task"}),
                None,
            ),
            2_000,
        )
        .unwrap();
        assert_eq!(resolved, identity);
        for scope in ["invocation:11:other-child", "invocation:12:child-task"] {
            let missing = execute(
                &mut database,
                &request(
                    "invocation.resolve_child",
                    11,
                    json!({"user_id": 11, "archive_scope": scope}),
                    None,
                ),
                2_000,
            )
            .unwrap_err();
            assert_eq!(missing.code(), "database_not_found");
        }

        // Missing conversation and missing parent fail closed.
        let no_conversation = register_child(
            &mut database,
            "missing-conv",
            "parent-task",
            "child-2",
            "reg-4",
        )
        .unwrap_err();
        assert_eq!(no_conversation.code(), "database_not_found");
        let no_parent = register_child(
            &mut database,
            "conv-1",
            "missing-parent",
            "child-2",
            "reg-5",
        )
        .unwrap_err();
        assert_eq!(no_parent.code(), "database_not_found");
    }

    #[test]
    fn task_result_checkpoint_enforces_child_identity_guard() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        create_conversation(&mut database, "conv-1");
        checkpoint_task(
            &mut database,
            "parent-task",
            json!({"task_id": "parent-task", "user_id": 11, "conv_id": "conv-1", "status": "running"}),
            0,
            "parent-birth",
        )
        .unwrap();
        register_child(
            &mut database,
            "conv-1",
            "parent-task",
            "child-task",
            "reg-1",
        )
        .unwrap();

        // A checkpoint preserving the exact birth identity advances.
        let preserved = checkpoint_task(
            &mut database,
            "child-task",
            json!({
                "task_id": "child-task",
                "user_id": 11,
                "conv_id": "conv-1",
                "status": "running",
                "created_at": 2,
                "child_invocation": birth_identity("parent-task"),
            }),
            1,
            "child-checkpoint-1",
        )
        .unwrap();
        assert_eq!(preserved["version"], json!(2));

        // Dropping or altering the identity is immutable-identity conflict.
        for forged in [
            json!({"task_id": "child-task", "user_id": 11, "conv_id": "conv-1", "status": "running"}),
            json!({
                "task_id": "child-task",
                "user_id": 11,
                "conv_id": "conv-1",
                "status": "running",
                "child_invocation": birth_identity("other-parent"),
            }),
        ] {
            let conflict =
                checkpoint_task(&mut database, "child-task", forged, 2, "child-checkpoint-x")
                    .unwrap_err();
            assert_eq!(conflict.code(), "database_conflict");
        }

        // A generic write may never introduce a birth identity by itself.
        let forge = checkpoint_task(
            &mut database,
            "forged-child",
            json!({
                "task_id": "forged-child",
                "user_id": 11,
                "conv_id": "conv-1",
                "status": "registered",
                "child_invocation": birth_identity("parent-task"),
            }),
            0,
            "child-forge",
        )
        .unwrap_err();
        assert_eq!(forge.code(), "database_protocol_error");
    }

    #[test]
    fn system_schema_version_reports_the_negotiated_storage_authority() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let response = execute(
            &mut database,
            &request("system.schema_version", 11, json!({}), None),
            1_000,
        )
        .unwrap();
        assert_eq!(
            response,
            json!({"version": crate::generated_storage_operations::STORAGE_SCHEMA_VERSION})
        );
        assert_eq!(
            execute(
                &mut database,
                &request("system.schema_version", 11, json!({"user_id": 11}), None,),
                1_001,
            )
            .unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
    }

    #[test]
    fn system_reclaim_reports_online_progress_without_io_or_deletion() {
        let vfs = prepared_vfs();
        let mut database = simulated_database(vfs.clone());
        database
            .commit_block_payload_for_test(b"reclaim-live")
            .unwrap();
        let orphan = database
            .write_orphan_block_for_test(b"reclaim-dead")
            .unwrap();
        // Without a daemon-installed collector the RPC still answers with
        // bounded latency and performs no physical I/O (the offline CLI
        // remains the exclusive lane there).
        for payload in [
            json!({}),
            json!({"min_free_pages": 0}),
            json!({"max_pages": 1, "min_free_pages": 0, "budget_ms": 10}),
        ] {
            vfs.arm_fault(None).unwrap();
            let result = execute(
                &mut database,
                &request("system.reclaim", 11, payload, Some("reclaim-online")),
                1_000,
            )
            .unwrap();
            assert_eq!(result["offline_required"], false);
            assert_eq!(result["reason_code"], serde_json::Value::Null);
            assert_eq!(result["statistics_complete"], false);
            assert_eq!(result["reclaimed"], 0);
            assert_eq!(result["more_candidates"], false);
            assert!(
                vfs.trace().unwrap().is_empty(),
                "online RPC must not start a graph walk"
            );
        }
        // With a collector handle installed the RPC rings the doorbell and
        // still performs no physical I/O itself.
        let collector = crate::authority_gc_online::OnlineGarbageCollector::new(
            AuthorityGarbageCollectionBudget::conservative(),
        );
        let handle = collector.handle();
        database.set_online_garbage_collection(handle.clone());
        vfs.arm_fault(None).unwrap();
        let result = execute(
            &mut database,
            &request("system.reclaim", 11, json!({}), Some("reclaim-doorbell")),
            1_000,
        )
        .unwrap();
        assert_eq!(result["offline_required"], false);
        assert_eq!(result["statistics_complete"], false);
        assert!(vfs.trace().unwrap().is_empty());
        handle.request_expedited_epoch();
        assert!(database.read_block_for_test(orphan).is_ok());
        // The explicit offline lane retains the actual collection capability.
        let collected = database
            .collect_garbage(AuthorityGarbageCollectionBudget::conservative())
            .unwrap();
        assert_eq!(collected.removed_blocks, 1);
    }

    #[test]
    fn system_reclaim_validates_legacy_bounds_before_storage_io() {
        let vfs = prepared_vfs();
        let mut database = simulated_database(vfs.clone());
        vfs.arm_fault(None).unwrap();
        let invalid = request(
            "system.reclaim",
            11,
            json!({"max_pages": 0, "min_free_pages": 0, "budget_ms": 10}),
            Some("system-reclaim-invalid"),
        );
        assert_eq!(
            execute(&mut database, &invalid, 1_000).unwrap_err(),
            SemanticExecutionError::InvalidPayload
        );
        assert!(vfs.trace().unwrap().is_empty());
    }

    fn rate_limit_request(event_id: &str, command_id: &str) -> Request {
        request(
            "rate_limit.record_and_check",
            11,
            json!({
                "endpoint": "/fault-rate-limit",
                "client_key": "198.51.100.40",
                "event_id": event_id,
                "limit": 1,
                "per_seconds": 604800,
            }),
            Some(command_id),
        )
    }

    #[test]
    fn rate_limit_is_receipted_owner_scoped_and_exact_at_the_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let first = rate_limit_request("rate-event-1", "rate-command-1");
        let allowed = execute(&mut database, &first, 1_000).unwrap();
        assert_eq!(allowed, json!({"allowed":true,"count":1,"pruned":0}));
        assert_eq!(execute(&mut database, &first, 2_000).unwrap(), allowed);
        assert_eq!(
            execute(
                &mut database,
                &rate_limit_request("rate-event-2", "rate-command-2"),
                2_001,
            )
            .unwrap(),
            json!({"allowed":false,"count":1,"pruned":0})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );

        let mut other_owner = rate_limit_request("rate-event-owner-12", "rate-command-owner-12");
        other_owner.owner_id = 12;
        assert_eq!(
            execute(&mut database, &other_owner, 2_002).unwrap(),
            json!({"allowed":true,"count":1,"pruned":0})
        );
    }

    fn assert_rate_limit_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 1).unwrap(),
            json!({"allowed":true,"count":1,"pruned":0})
        );
        assert_eq!(
            execute(
                &mut recovered,
                &rate_limit_request("fault-rate-event-2", "fault-rate-command-2"),
                2,
            )
            .unwrap(),
            json!({"allowed":false,"count":1,"pruned":0})
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_rate_limit_commit_fault_recovers_event_counters_receipt_and_outbox_atomically() {
        let command = rate_limit_request("fault-rate-event-1", "fault-rate-command-1");
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 1).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1);
            drop(database);
            assert_rate_limit_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1);
            drop(database);
            assert_rate_limit_fault_replay_converges(vfs, &command);
        }
    }

    fn prepared_vfs() -> std::sync::Arc<DeterministicVfs> {
        let vfs = std::sync::Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(std::path::Path::new("/data")).unwrap();
        vfs.sync_directory(std::path::Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn simulated_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn provider_fault_create() -> Request {
        request(
            "provider.create",
            11,
            json!({
                "owner_user_id": 11,
                "tenant_id": "fault-label",
                "provider_id": "fault-provider",
                "name": "Fault Provider",
                "base_url": "https://provider.invalid/v1",
                "api_key_ciphertext": "sealed",
                "key_hint": "…aled",
                "models": [],
                "extra_headers": {},
                "thinking_format": "",
                "created_at": 10.0
            }),
            Some("provider-fault-create"),
        )
    }

    fn assert_provider_create_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let created = execute(&mut recovered, command, 20_000).unwrap();
        assert_eq!(created["id"], "fault-provider");
        let listed = execute(
            &mut recovered,
            &request(
                "provider.list",
                11,
                json!({"owner_user_id": 11, "tenant_id": "fault-label"}),
                None,
            ),
            20_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_provider_create_commit_fault_recovers_one_atomic_semantic_prefix() {
        let command = provider_fault_create();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 10_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 10_000);
            drop(database);
            assert_provider_create_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 10_000);
            drop(database);
            assert_provider_create_replay_converges(vfs, &command);
        }
    }

    fn model_routing_fault_commit(revision: u64, command_id: &str) -> Request {
        request(
            "model_routing.commit",
            51,
            json!({
                "owner_user_id": 51,
                "tenant_id": "fault-routing",
                "expected_revision": revision - 1,
                "document": {
                    "contract_version": "tofu.model-routing/v2",
                    "revision": revision,
                    "connections": [],
                    "credentials": [],
                    "models": [],
                    "providers": [],
                    "routes": [],
                    "diagnostic_padding": if revision == 1 {
                        "old".repeat(7_000)
                    } else {
                        "new".repeat(7_000)
                    }
                },
                "migration_receipt": {
                    "status": "committed",
                    "revision": revision,
                    "evidence": "receipt".repeat(3_000)
                },
                "updated_at": revision as f64
            }),
            Some(command_id),
        )
    }

    fn seeded_model_routing_fault_database() -> (std::sync::Arc<DeterministicVfs>, AuthorityDatabase)
    {
        let vfs = prepared_vfs();
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &model_routing_fault_commit(1, "routing-fault-seed"),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, database)
    }

    fn assert_model_routing_commit_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 2_000).unwrap()["revision"],
            2
        );
        let get = request(
            "model_routing.get",
            51,
            json!({"owner_user_id": 51, "tenant_id": "fault-routing"}),
            None,
        );
        let authority = execute(&mut recovered, &get, 2_001).unwrap();
        assert_eq!(authority["revision"], 2);
        assert!(authority["document"]["diagnostic_padding"]
            .as_str()
            .unwrap()
            .starts_with("new"));
        let receipt = request(
            "model_routing.migration_receipt",
            51,
            json!({"owner_user_id": 51, "tenant_id": "fault-routing"}),
            None,
        );
        let migration = execute(&mut recovered, &receipt, 2_002).unwrap();
        assert_eq!(migration["receipt"]["revision"], 2);
        assert_eq!(migration["backup"]["revision"], 1);
        assert!(migration["backup"]["diagnostic_padding"]
            .as_str()
            .unwrap()
            .starts_with("old"));
        assert_eq!(
            recovered
                .logical_outbox_status(7, 51)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_model_routing_migration_commit_fault_recovers_one_complete_revision() {
        let command = model_routing_fault_commit(2, "routing-fault-migrate");
        let (baseline_vfs, mut baseline) = seeded_model_routing_fault_database();
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut database) = seeded_model_routing_fault_database();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_model_routing_commit_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut database) = seeded_model_routing_fault_database();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_model_routing_commit_replay_converges(vfs, &command);
        }
    }

    fn model_routing_receipt_only_fault_command() -> Request {
        request(
            "model_routing.migration_receipt.put",
            51,
            json!({
                "owner_user_id": 51,
                "tenant_id": "fault-routing",
                "migration_receipt": {
                    "status": "rejected",
                    "evidence": "receipt-only".repeat(3_000)
                },
                "updated_at": 2.0
            }),
            Some("routing-fault-receipt-only"),
        )
    }

    fn assert_model_routing_receipt_only_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let acknowledgement = execute(&mut recovered, command, 2_000).unwrap();
        assert_eq!(acknowledgement["revision"], 1);
        assert_eq!(acknowledgement["status"], "rejected");
        let authority = execute(
            &mut recovered,
            &request(
                "model_routing.get",
                51,
                json!({"owner_user_id": 51, "tenant_id": "fault-routing"}),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(authority["revision"], 1);
        assert!(authority["document"]["diagnostic_padding"]
            .as_str()
            .unwrap()
            .starts_with("old"));
        let migration = execute(
            &mut recovered,
            &request(
                "model_routing.migration_receipt",
                51,
                json!({"owner_user_id": 51, "tenant_id": "fault-routing"}),
                None,
            ),
            2_002,
        )
        .unwrap();
        assert_eq!(migration["receipt"]["status"], "rejected");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 51)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_model_routing_receipt_only_fault_preserves_the_route_document() {
        let command = model_routing_receipt_only_fault_command();
        let (baseline_vfs, mut baseline) = seeded_model_routing_fault_database();
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut database) = seeded_model_routing_fault_database();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_model_routing_receipt_only_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut database) = seeded_model_routing_fault_database();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_model_routing_receipt_only_replay_converges(vfs, &command);
        }
    }

    fn artifact_fault_create() -> Request {
        request(
            "artifact.create",
            61,
            json!({
                "artifact_id": "fault-artifact",
                "conv_id": "fault-conversation",
                "task_id": "fault-task",
                "msg_id": "fault-message",
                "source": "write_file",
                "source_ref": {"path":"fault.md"},
                "format": "markdown",
                "title": "fault.md",
                "content": "fault-content".repeat(2_000),
                "meta": {"fault":true},
                "created_at": 1_000
            }),
            Some("artifact-fault-create"),
        )
    }

    fn assert_artifact_create_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let response = execute(&mut recovered, command, 20_000).unwrap();
        assert_eq!(response["created"], true);
        assert_eq!(response["artifact"]["id"], "fault-artifact");
        let full = execute(
            &mut recovered,
            &request(
                "artifact.get",
                61,
                json!({"artifact_id":"fault-artifact","include_content":true}),
                None,
            ),
            20_001,
        )
        .unwrap();
        assert_eq!(full["content"], "fault-content".repeat(2_000));
        let listed = execute(
            &mut recovered,
            &request(
                "artifact.list",
                61,
                json!({"conv_id":"fault-conversation","include_deleted":false}),
                None,
            ),
            20_002,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 61)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_artifact_semantic_commit_fault_recovers_blob_indexes_receipt_and_outbox() {
        let command = artifact_fault_create();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 10_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 10_000);
            drop(database);
            assert_artifact_create_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 10_000);
            drop(database);
            assert_artifact_create_replay_converges(vfs, &command);
        }
    }

    fn worker_fault_enqueue() -> Request {
        request(
            "worker_job.enqueue",
            41,
            json!({
                "task_id": "fault-worker-job",
                "user_id": 41,
                "tenant_id": "fault-tenant",
                "task_kind": "conversation-attempt",
                "idempotency_key": "fault-worker-key",
                "payload": {"large": "w".repeat(20_000)},
                "priority": 7,
                "available_at_ms": 1_000,
                "now_ms": 1_000
            }),
            Some("worker-fault-enqueue"),
        )
    }

    fn worker_fault_claim(command_id: &str) -> Request {
        request(
            "worker_job.claim_next",
            90,
            json!({
                "worker_id": "fault-worker",
                "task_kinds": ["conversation-attempt"],
                "now_ms": 1_001,
                "lease_ms": 60_000
            }),
            Some(command_id),
        )
    }

    fn assert_worker_enqueue_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 1_000).unwrap();
        assert_eq!(replayed["job"]["taskId"], "fault-worker-job");
        assert_eq!(
            replayed["job"]["payload"]["large"].as_str().unwrap().len(),
            20_000
        );
        let claimed = execute(
            &mut recovered,
            &worker_fault_claim("worker-fault-enqueue-claim"),
            1_001,
        )
        .unwrap();
        assert_eq!(claimed["taskId"], "fault-worker-job");
        assert_eq!(claimed["fencingToken"], 1);
    }

    #[test]
    fn every_worker_enqueue_commit_fault_recovers_document_queue_blob_and_receipt_atomically() {
        let command = worker_fault_enqueue();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 1_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_000);
            drop(database);
            assert_worker_enqueue_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_000);
            drop(database);
            assert_worker_enqueue_replay_converges(vfs, &command);
        }
    }

    fn seeded_worker_claim_database() -> (std::sync::Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = prepared_vfs();
        let mut database = simulated_database(vfs.clone());
        execute(&mut database, &worker_fault_enqueue(), 1_000).unwrap();
        vfs.arm_fault(None).unwrap();
        (vfs, database)
    }

    fn assert_worker_claim_recovery_converges(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let get = request(
            "worker_job.get",
            41,
            json!({"task_id": "fault-worker-job", "user_id": 41}),
            None,
        );
        let before_retry = execute(&mut recovered, &get, 1_001).unwrap();
        if before_retry["status"] == "queued" {
            let claimed = execute(
                &mut recovered,
                &worker_fault_claim("worker-fault-claim-retry"),
                1_001,
            )
            .unwrap();
            assert_eq!(claimed["taskId"], "fault-worker-job");
        } else {
            assert_eq!(before_retry["status"], "running");
        }
        let final_job = execute(&mut recovered, &get, 1_002).unwrap();
        assert_eq!(final_job["status"], "running");
        assert_eq!(final_job["fencingToken"], 1);
        assert_eq!(final_job["claimOwner"], "fault-worker");
    }

    #[test]
    fn every_worker_claim_commit_fault_recovers_one_fenced_queue_prefix() {
        let (baseline_vfs, mut baseline) = seeded_worker_claim_database();
        execute(
            &mut baseline,
            &worker_fault_claim("worker-fault-claim-baseline"),
            1_001,
        )
        .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let (vfs, mut database) = seeded_worker_claim_database();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &worker_fault_claim("worker-fault-claim-attempt"),
                1_001,
            );
            drop(database);
            assert_worker_claim_recovery_converges(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let (vfs, mut database) = seeded_worker_claim_database();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &worker_fault_claim("worker-fault-claim-attempt"),
                1_001,
            );
            drop(database);
            assert_worker_claim_recovery_converges(vfs);
        }
    }

    fn assert_conversation_create_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"applied": true, "rev": 0})
        );
        let count = request("conversation.count", 11, json!({"user_id": 11}), None);
        assert_eq!(
            execute(&mut recovered, &count, 3_001).unwrap(),
            json!({"count": 1})
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "conversation.activity_dates",
                    11,
                    json!({
                        "user_id": 11,
                        "updated_at_gte": 0,
                        "day_boundaries_ms": [0, 4_000]
                    }),
                    None,
                ),
                3_002,
            )
            .unwrap(),
            json!({"candidate_count": 1, "counts": [0]})
        );
        let duplicate = request(
            "conversation.create",
            12,
            json!({"conv_id": "fault-conversation", "user_id": 12}),
            Some("fault-conversation-other-owner"),
        );
        assert_eq!(
            execute(&mut recovered, &duplicate, 3_002).unwrap_err(),
            SemanticExecutionError::Conflict
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    fn seeded_conversation_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        let create = request(
            "conversation.create",
            11,
            json!({"conv_id": "fault-update", "user_id": 11}),
            Some("fault-update-create"),
        );
        execute(&mut database, &create, 1_000).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn seeded_delete_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "fault-lifecycle", "user_id": 11}),
                Some("fault-lifecycle-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "fault-lifecycle",
                    "user_id": 11,
                    "command_id": "fault-lifecycle-append",
                    "actor": "assistant",
                    "projection": {"content": "z".repeat(20 * 1024)},
                    "turn_id": "fault-lifecycle-turn"
                }),
                Some("fault-lifecycle-append"),
            ),
            2_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn clone_fault_request() -> Request {
        request(
            "conversation.clone",
            11,
            json!({
                "conv_id": "fault-clone-source",
                "destination_conv_id": "fault-clone-destination",
                "user_id": 11,
                "title": "Fault clone"
            }),
            Some("fault-clone-command"),
        )
    }

    fn archive_fault_request() -> Request {
        request(
            "compaction_archive.create",
            11,
            json!({
                "archive_id": "fault-archive",
                "conversation_id": "fault-archive-conversation",
                "user_id": 11,
                "messages": [{"role": "assistant", "content": "blob".repeat(20 * 1024)}],
                "summary": "durable summary",
                "receipt": {"status": "completed"},
                "task_id": "fault-archive-task",
                "msgs_before": 1,
                "created_at_ms": 2_000
            }),
            Some("fault-archive-create"),
        )
    }

    fn seeded_archive_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id": "fault-archive-conversation", "user_id": 11}),
                Some("fault-archive-conversation-create"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_archive_create_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"created": true, "archiveId": "fault-archive"})
        );
        let loaded = execute(
            &mut recovered,
            &request(
                "compaction_archive.get",
                11,
                json!({
                    "conversation_id": "fault-archive-conversation",
                    "archive_id": "fault-archive",
                    "user_id": 11
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(loaded["archive"]["summary"], "durable summary");
        assert_eq!(loaded["archive"]["resultStatus"], "completed");
        assert_eq!(loaded["messages"].as_array().unwrap().len(), 1);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    fn seeded_committed_archive_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = seeded_archive_database(vfs.clone());
        execute(&mut database, &archive_fault_request(), 2_000).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn archive_summary_fault_request() -> Request {
        request(
            "compaction_archive.update_summary",
            11,
            json!({
                "archive_id": "fault-archive",
                "user_id": 11,
                "summary": "replacement summary",
                "tokens_after": 123,
                "msgs_after": 4,
                "receipt": {"status": "completed", "strategy": "fault-proof"}
            }),
            Some("fault-archive-summary-update"),
        )
    }

    fn assert_archive_summary_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"updated": true})
        );
        let loaded = execute(
            &mut recovered,
            &request(
                "compaction_archive.get",
                11,
                json!({"conversation_id": "fault-archive-conversation", "archive_id": "fault-archive", "user_id": 11}),
                None,
            ),
            3_001,
        ).unwrap();
        assert_eq!(loaded["archive"]["summary"], "replacement summary");
        assert_eq!(loaded["archive"]["tokensAfter"], 123);
        assert_eq!(loaded["archive"]["msgsAfter"], 4);
        assert_eq!(loaded["archive"]["resultStrategy"], "fault-proof");
        assert_eq!(loaded["messages"].as_array().unwrap().len(), 1);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            3
        );
    }

    fn archive_delete_fault_request() -> Request {
        request(
            "compaction_archive.delete_conversation",
            11,
            json!({"conversation_id": "fault-archive-conversation", "user_id": 11}),
            Some("fault-archive-delete"),
        )
    }

    fn assert_archive_delete_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"deleted": 1})
        );
        let listed = execute(
            &mut recovered,
            &request(
                "compaction_archive.list",
                11,
                json!({"conversation_id": "fault-archive-conversation", "user_id": 11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed, json!({"archives": []}));
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            3
        );
    }

    fn exercise_archive_fault_matrix(
        command: &Request,
        assert_replay: fn(std::sync::Arc<DeterministicVfs>, &Request),
    ) {
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_committed_archive_database(baseline_vfs.clone());
        execute(&mut baseline, command, 3_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_committed_archive_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, command, 3_000);
            drop(database);
            assert_replay(vfs, command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_committed_archive_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, command, 3_000);
            drop(database);
            assert_replay(vfs, command);
        }
    }

    fn seeded_clone_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "fault-clone-source",
                    "user_id": 11,
                    "settings": {"activeTaskId": "must-not-clone"}
                }),
                Some("fault-clone-source-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "fault-clone-source",
                    "user_id": 11,
                    "command_id": "fault-clone-source-turn-command",
                    "actor": "assistant",
                    "projection": {
                        "content": "z".repeat(20 * 1024),
                        "timestamp": 200,
                        "toolRounds": [{"taskId": "fault-source-task", "status": "done"}]
                    },
                    "turn_id": "fault-clone-source-turn"
                }),
                Some("fault-clone-source-turn"),
            ),
            2_000,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "compaction_archive.create",
                11,
                json!({
                    "archive_id": "fault-clone-archive",
                    "conversation_id": "fault-clone-source",
                    "user_id": 11,
                    "messages": [{"role": "assistant", "content": "archive".repeat(10 * 1024)}],
                    "summary": "fault summary",
                    "task_id": "fault-source-task",
                    "msgs_before": 1,
                    "created_at_ms": 2_500
                }),
                Some("fault-clone-archive-create"),
            ),
            2_500,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_conversation_clone_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert!(execute(&mut recovered, command, 3_000).unwrap()["cloned"]
            .as_bool()
            .unwrap());
        let source = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-clone-source", "user_id": 11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        let destination = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-clone-destination", "user_id": 11}),
                None,
            ),
            3_002,
        )
        .unwrap();
        assert_eq!(source["messages"][0]["_turnId"], "fault-clone-source-turn");
        assert_ne!(
            destination["messages"][0]["_turnId"],
            "fault-clone-source-turn"
        );
        assert_eq!(destination["messages"][0]["content"], "z".repeat(20 * 1024));
        assert!(destination["messages"][0]["_attemptId"].is_null());
        assert_eq!(
            destination["metadata"]["settings"],
            json!({"clonedFrom": "fault-clone-source"})
        );
        let count = request("conversation.count", 11, json!({"user_id": 11}), None);
        assert_eq!(execute(&mut recovered, &count, 3_003).unwrap()["count"], 2);
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "conversation.activity_dates",
                    11,
                    json!({
                        "user_id": 11,
                        "updated_at_gte": 0,
                        "day_boundaries_ms": [0, 250, 4_000]
                    }),
                    None,
                ),
                3_004,
            )
            .unwrap(),
            json!({"candidate_count": 2, "counts": [2, 0]})
        );
        let source_archives = execute(
            &mut recovered,
            &request(
                "compaction_archive.list",
                11,
                json!({"conversation_id": "fault-clone-source", "user_id": 11}),
                None,
            ),
            3_004,
        )
        .unwrap();
        let destination_archives = execute(
            &mut recovered,
            &request(
                "compaction_archive.list",
                11,
                json!({"conversation_id": "fault-clone-destination", "user_id": 11}),
                None,
            ),
            3_005,
        )
        .unwrap();
        assert_eq!(source_archives["archives"].as_array().unwrap().len(), 1);
        assert_eq!(
            destination_archives["archives"].as_array().unwrap().len(),
            1
        );
        assert_ne!(
            source_archives["archives"][0]["id"],
            destination_archives["archives"][0]["id"]
        );
        assert_ne!(
            source_archives["archives"][0]["taskId"],
            destination_archives["archives"][0]["taskId"]
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            4
        );
    }

    fn seeded_restore_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = seeded_delete_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "conversation.delete",
                11,
                json!({"conv_id": "fault-lifecycle", "user_id": 11}),
                Some("fault-lifecycle-delete"),
            ),
            3_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn seeded_trash_prune_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        for (conversation_id, deleted_at_ms) in [("fault-prune-a", 2_000), ("fault-prune-b", 3_000)]
        {
            execute(
                &mut database,
                &request(
                    "conversation.create",
                    11,
                    json!({"conv_id": conversation_id, "user_id": 11}),
                    Some(&format!("fault-create-{conversation_id}")),
                ),
                1_000,
            )
            .unwrap();
            execute(
                &mut database,
                &request(
                    "conversation.delete",
                    11,
                    json!({"conv_id": conversation_id, "user_id": 11}),
                    Some(&format!("fault-delete-{conversation_id}")),
                ),
                deleted_at_ms,
            )
            .unwrap();
        }
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_conversation_delete_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let result = execute(&mut recovered, command, 4_000).unwrap();
        assert_eq!(result["deleted"], true);
        assert_eq!(result["recoverable"], true);
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "conversation.get",
                    11,
                    json!({"conv_id": "fault-lifecycle", "user_id": 11}),
                    None,
                ),
                4_001,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request("conversation.count", 11, json!({"user_id": 11}), None),
                4_002,
            )
            .unwrap(),
            json!({"count": 0})
        );
        let pin_id = {
            let transaction = recovered.begin(7, 11).unwrap();
            crate::conversation_header::trash_pin_id(&transaction, "fault-lifecycle")
        };
        let mut trash = recovered
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .unwrap();
        assert_eq!(
            crate::turn::list_values_until(&recovered, &mut trash, "fault-lifecycle", None, &mut || false)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            3
        );
    }

    fn assert_conversation_restore_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 5_000).unwrap(),
            json!({
                "restored": true,
                "conflict": false,
                "missing": false,
                "rev": 2,
                "turnCount": 1
            })
        );
        let conversation = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-lifecycle", "user_id": 11}),
                None,
            ),
            5_001,
        )
        .unwrap();
        assert_eq!(conversation["metadata"]["rev"], 2);
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({"conversation_id": "fault-lifecycle", "user_id": 11}),
                None,
            ),
            5_002,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 1);
        assert_eq!(turns[0]["currentAttemptId"], Value::Null);
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "conversation.activity_dates",
                    11,
                    json!({
                        "user_id": 11,
                        "updated_at_gte": 0,
                        "day_boundaries_ms": [0, 6_000]
                    }),
                    None,
                ),
                5_003,
            )
            .unwrap(),
            json!({"candidate_count": 1, "counts": [1]})
        );
        let pin_id = {
            let transaction = recovered.begin(7, 11).unwrap();
            crate::conversation_header::trash_pin_id(&transaction, "fault-lifecycle")
        };
        assert!(recovered
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .is_none());
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            4
        );
    }

    fn assert_conversation_purge_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 5_000).unwrap(),
            json!({"purged": true})
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "conversation.restore",
                    11,
                    json!({"conv_id": "fault-lifecycle", "user_id": 11}),
                    Some("fault-purge-restore")
                ),
                5_001,
            )
            .unwrap(),
            json!({"restored": false, "conflict": false, "missing": true})
        );
        let pin_id = {
            let transaction = recovered.begin(7, 11).unwrap();
            crate::conversation_header::trash_pin_id(&transaction, "fault-lifecycle")
        };
        assert!(recovered
            .begin_persistent_snapshot(7, 11, &pin_id)
            .unwrap()
            .is_none());
        let mut claims = recovered.begin_with_identity_claim_scopes(7, 11).unwrap();
        for (namespace, identity) in [
            (
                crate::generated_tofudb_ir::CONVERSATION_ID_CLAIM_NAMESPACE,
                "fault-lifecycle",
            ),
            (
                crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
                "fault-lifecycle-turn",
            ),
        ] {
            let key = EntityKey::new(7, u64::MAX, namespace, identity.as_bytes()).unwrap();
            assert!(recovered.entity_get(&mut claims, &key).unwrap().is_none());
        }
    }

    fn assert_trash_prune_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 5_000).unwrap();
        execute(&mut recovered, command, 5_001).unwrap();
        for conversation_id in ["fault-prune-a", "fault-prune-b"] {
            assert_eq!(
                execute(
                    &mut recovered,
                    &request(
                        "conversation.restore",
                        11,
                        json!({"conv_id": conversation_id, "user_id": 11}),
                        Some(&format!("fault-restore-{conversation_id}"))
                    ),
                    5_002,
                )
                .unwrap(),
                json!({"restored": false, "conflict": false, "missing": true})
            );
        }
    }

    fn assert_conversation_metadata_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"applied": true, "missing": false, "rev": 0})
        );
        let get = request(
            "conversation.get",
            11,
            json!({"conv_id": "fault-update", "user_id": 11}),
            None,
        );
        let document = execute(&mut recovered, &get, 3_001).unwrap();
        assert_eq!(document["metadata"]["title"], "after-fault");
        assert_eq!(document["metadata"]["updated_at"], 2_000);
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "conversation.activity_dates",
                    11,
                    json!({
                        "user_id": 11,
                        "updated_at_gte": 1_500,
                        "day_boundaries_ms": [0, 4_000]
                    }),
                    None,
                ),
                3_002,
            )
            .unwrap(),
            json!({"candidate_count": 1, "counts": [0]})
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_conversation_create_commit_fault_recovers_all_semantic_effects_atomically() {
        let command = request(
            "conversation.create",
            11,
            json!({
                "conv_id": "fault-conversation",
                "user_id": 11,
                "settings": {"model": "test"}
            }),
            Some("fault-conversation-create"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_conversation_create_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_conversation_create_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_conversation_clone_commit_fault_recovers_one_inert_snapshot() {
        let command = clone_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_clone_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 3_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_clone_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_conversation_clone_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_clone_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_conversation_clone_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_compaction_archive_create_fault_recovers_one_complete_history_record() {
        let command = archive_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_archive_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_archive_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_archive_create_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_archive_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_archive_create_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_compaction_archive_summary_fault_recovers_old_or_new_complete_metadata() {
        let command = archive_summary_fault_request();
        exercise_archive_fault_matrix(&command, assert_archive_summary_replay_converges);
    }

    #[test]
    fn every_compaction_archive_delete_fault_recovers_one_complete_collection_state() {
        let command = archive_delete_fault_request();
        exercise_archive_fault_matrix(&command, assert_archive_delete_replay_converges);
    }

    #[test]
    fn every_conversation_delete_commit_fault_recovers_one_atomic_lifecycle_state() {
        let command = request(
            "conversation.delete",
            11,
            json!({"conv_id": "fault-lifecycle", "user_id": 11}),
            Some("fault-lifecycle-delete"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_delete_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 3_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_delete_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_conversation_delete_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_delete_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_conversation_delete_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_conversation_restore_commit_fault_recovers_one_inert_active_graph() {
        let command = request(
            "conversation.restore",
            11,
            json!({"conv_id": "fault-lifecycle", "user_id": 11}),
            Some("fault-lifecycle-restore"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_restore_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 4_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_conversation_restore_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_conversation_restore_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_conversation_purge_commit_fault_recovers_one_absent_identity_graph() {
        let command = request(
            "conversation.purge",
            11,
            json!({"conv_id": "fault-lifecycle", "user_id": 11}),
            Some("fault-lifecycle-purge"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_restore_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 4_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_conversation_purge_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_restore_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_conversation_purge_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_trash_prune_commit_fault_converges_without_a_partial_conversation_purge() {
        let command = request(
            "conversation.trash.prune",
            11,
            json!({"deleted_before_ms": 4_000, "max_conversations": 1}),
            Some("fault-trash-prune"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_trash_prune_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 4_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_trash_prune_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_trash_prune_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_trash_prune_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_trash_prune_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_conversation_metadata_commit_fault_recovers_header_dirty_receipt_and_outbox() {
        let command = request(
            "conversation.metadata.update",
            11,
            json!({
                "conv_id": "fault-update",
                "user_id": 11,
                "updates": {"title": "after-fault", "updated_at": 2_000}
            }),
            Some("fault-metadata-update"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_conversation_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_conversation_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_conversation_metadata_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_conversation_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_conversation_metadata_replay_converges(vfs, &command);
        }
    }

    fn assert_settled_turn_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replay["turn"]["turnId"], "fault-turn");
        assert_eq!(replay["turn"]["ordinal"], 0);
        assert_eq!(replay["conversationRevision"], 1);
        assert_eq!(
            replay["turn"]["projection"]["content"]
                .as_str()
                .unwrap()
                .len(),
            20_000
        );
        let turns = request(
            "turn.list",
            11,
            json!({"conversation_id": "fault-turn-conversation", "user_id": 11}),
            None,
        );
        let rows = execute(&mut recovered, &turns, 3_001).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0], replay["turn"]);
        let delta = request(
            "turn.list_delta",
            11,
            json!({
                "conversation_id": "fault-turn-conversation",
                "user_id": 11,
                "since_ms": 0
            }),
            None,
        );
        let delta = execute(&mut recovered, &delta, 3_001).unwrap();
        assert_eq!(delta["turns"], json!([replay["turn"]]));
        assert_eq!(delta["deletedTurnIds"], json!([]));
        let conversation = request(
            "conversation.get",
            11,
            json!({"conv_id": "fault-turn-conversation", "user_id": 11}),
            None,
        );
        let document = execute(&mut recovered, &conversation, 3_002).unwrap();
        assert_eq!(document["metadata"]["msg_count"], 1);
        assert_eq!(document["metadata"]["rev"], 1);
        assert_eq!(document["messages"][0]["_turnId"], "fault-turn");
        let activity = execute(
            &mut recovered,
            &request(
                "conversation.activity_dates",
                11,
                json!({
                    "user_id": 11,
                    "updated_at_gte": 0,
                    "day_boundaries_ms": [0, 4_000]
                }),
                None,
            ),
            3_003,
        )
        .unwrap();
        assert_eq!(activity, json!({"candidate_count": 1, "counts": [1]}));
        let mut claim_scope = recovered.begin_with_identity_claim_scopes(7, 12).unwrap();
        let turn_claim = EntityKey::new(
            7,
            u64::MAX,
            crate::generated_tofudb_ir::TURN_ID_CLAIM_NAMESPACE,
            b"fault-turn",
        )
        .unwrap();
        assert_eq!(
            recovered.entity_get(&mut claim_scope, &turn_claim).unwrap(),
            Some(11_u64.to_be_bytes().to_vec())
        );
        let attempt_id = replay["attempt"]["attemptId"].as_str().unwrap();
        let attempt_claim = EntityKey::new(
            7,
            u64::MAX,
            crate::generated_tofudb_ir::ATTEMPT_ID_CLAIM_NAMESPACE,
            attempt_id.as_bytes(),
        )
        .unwrap();
        assert_eq!(
            recovered
                .entity_get(&mut claim_scope, &attempt_claim)
                .unwrap(),
            Some(crate::turn::encode_attempt_locator(11, "fault-turn-conversation").unwrap())
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    fn seeded_turn_delete_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        let append = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "fault-delete-conversation",
                "user_id": 11,
                "command_id": "fault-delete-append",
                "actor": "human",
                "projection": {"content": "delete me"},
                "turn_id": "fault-delete-turn",
                "conversation_defaults": {
                    "allowCreate": true,
                    "title": "Fault Delete",
                    "settings": {}
                }
            }),
            Some("fault-delete-append"),
        );
        execute(&mut database, &append, 1_000).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn turn_compaction_fault_request() -> Request {
        request(
            "turn.compact",
            11,
            json!({
                "conversation_id": "fault-compact-conversation",
                "user_id": 11,
                "expected_conversation_revision": 3,
                "summary_turn_id": "fault-compact-summary",
                "summary_projection": {
                    "content": "folded history",
                    "thinking": "",
                    "segments": [],
                    "toolRounds": [],
                    "compaction": {"blockId": "compaction"}
                },
                "delete_turn_ids": ["fault-compact-t1"],
                "projection_updates": [{
                    "turn_id": "fault-compact-t2",
                    "expected_projection_revision": 1,
                    "projection": {
                        "content": "retained",
                        "thinking": "",
                        "segments": [],
                        "toolRounds": []
                    }
                }],
                "insert_after_turn_id": "fault-compact-t0",
                "insert_before_turn_id": "fault-compact-t2"
            }),
            Some("fault-turn-compact"),
        )
    }

    fn seeded_turn_compaction_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        for (ordinal, turn_id) in [
            (0_u64, "fault-compact-t0"),
            (1, "fault-compact-t1"),
            (2, "fault-compact-t2"),
        ] {
            execute(
                &mut database,
                &request(
                    "turn.append_settled",
                    11,
                    json!({
                        "conversation_id": "fault-compact-conversation",
                        "user_id": 11,
                        "command_id": format!("fault-compact-append-{ordinal}"),
                        "actor": "human",
                        "projection": {"content": format!("turn {ordinal}")},
                        "turn_id": turn_id,
                        "conversation_defaults": {
                            "allowCreate": true,
                            "title": "Fault Compact",
                            "settings": {}
                        }
                    }),
                    Some(&format!("fault-compact-append-{ordinal}")),
                ),
                1_000 + ordinal,
            )
            .unwrap();
        }
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_turn_compaction_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 2_000).unwrap();
        assert_eq!(replay["applied"], true);
        assert_eq!(replay["conversationRevision"], 4);
        assert_eq!(replay["deletedTurnIds"], json!(["fault-compact-t1"]));
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({
                    "conversation_id": "fault-compact-conversation",
                    "user_id": 11
                }),
                None,
            ),
            2_001,
        )
        .unwrap();
        assert_eq!(turns.as_array().unwrap().len(), 3);
        assert_eq!(turns[0]["turnId"], "fault-compact-t0");
        assert_eq!(turns[1]["turnId"], "fault-compact-summary");
        assert_eq!(turns[2]["turnId"], "fault-compact-t2");
        assert_eq!(turns[2]["parentTurnId"], "fault-compact-summary");
        assert_eq!(turns[2]["projectionRevision"], 2);
    }

    fn assert_turn_delete_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turn = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-delete-conversation",
                    "user_id": 11,
                    "turn_id": "fault-delete-turn"
                }),
                None,
            ),
            3_000,
        )
        .unwrap();
        let conversation = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-delete-conversation", "user_id": 11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        let delta = execute(
            &mut recovered,
            &request(
                "turn.list_delta",
                11,
                json!({
                    "conversation_id": "fault-delete-conversation",
                    "user_id": 11,
                    "since_ms": 0
                }),
                None,
            ),
            3_002,
        )
        .unwrap();
        let snapshot = execute(
            &mut recovered,
            &request(
                "turn.sync.snapshot",
                11,
                json!({
                    "conversation_id": "fault-delete-conversation",
                    "user_id": 11,
                    "turn_limit": 1
                }),
                None,
            ),
            3_003,
        )
        .unwrap();
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        let activity = execute(
            &mut recovered,
            &request(
                "conversation.activity_dates",
                11,
                json!({
                    "user_id": 11,
                    "updated_at_gte": 0,
                    "day_boundaries_ms": [0, 4_000]
                }),
                None,
            ),
            3_004,
        )
        .unwrap();
        if turn.is_null() {
            assert_eq!(conversation["metadata"]["rev"], 2);
            assert_eq!(conversation["metadata"]["msg_count"], 0);
            assert_eq!(conversation["messages"], json!([]));
            assert_eq!(delta["turns"], json!([]));
            assert_eq!(delta["deletedTurnIds"], json!(["fault-delete-turn"]));
            assert_eq!(snapshot["syncSequence"], 2);
            assert_eq!(snapshot["turnWindow"]["totalTurns"], 0);
            assert_eq!(outbox_sequence, 2);
            assert_eq!(activity, json!({"candidate_count": 1, "counts": [0]}));
        } else {
            assert_eq!(turn["turnId"], "fault-delete-turn");
            assert_eq!(conversation["metadata"]["rev"], 1);
            assert_eq!(conversation["metadata"]["msg_count"], 1);
            assert_eq!(conversation["messages"].as_array().unwrap().len(), 1);
            assert_eq!(delta["turns"].as_array().unwrap().len(), 1);
            assert_eq!(delta["deletedTurnIds"], json!([]));
            assert_eq!(snapshot["syncSequence"], 1);
            assert_eq!(snapshot["turnWindow"]["totalTurns"], 1);
            assert_eq!(outbox_sequence, 1);
            assert_eq!(activity, json!({"candidate_count": 1, "counts": [1]}));
        }
    }

    fn seeded_projection_update_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "fault-edit-conversation",
                    "user_id": 11,
                    "command_id": "fault-edit-append",
                    "actor": "assistant",
                    "projection": {
                        "content": "before", "toolRounds": [], "timestamp": 100
                    },
                    "turn_id": "fault-edit-turn",
                    "conversation_defaults": {
                        "allowCreate": true,
                        "title": "Fault Edit",
                        "settings": {}
                    }
                }),
                Some("fault-edit-append"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_projection_update_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turn = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-edit-conversation",
                    "user_id": 11,
                    "turn_id": "fault-edit-turn"
                }),
                None,
            ),
            3_000,
        )
        .unwrap();
        let lane = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({
                    "conversation_id": "fault-edit-conversation",
                    "user_id": 11,
                    "lane_id": "main"
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        let conversation = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-edit-conversation", "user_id": 11}),
                None,
            ),
            3_002,
        )
        .unwrap();
        let snapshot = execute(
            &mut recovered,
            &request(
                "turn.sync.snapshot",
                11,
                json!({
                    "conversation_id": "fault-edit-conversation",
                    "user_id": 11,
                    "turn_limit": 1
                }),
                None,
            ),
            3_003,
        )
        .unwrap();
        assert_eq!(lane[0], turn);
        assert_eq!(
            conversation["messages"][0]["content"],
            turn["projection"]["content"]
        );
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        let activity = execute(
            &mut recovered,
            &request(
                "conversation.activity_dates",
                11,
                json!({
                    "user_id": 11,
                    "updated_at_gte": 0,
                    "day_boundaries_ms": [0, 150, 300]
                }),
                None,
            ),
            3_004,
        )
        .unwrap();
        match turn["projectionRevision"].as_u64().unwrap() {
            1 => {
                assert_eq!(turn["projection"]["content"], "before");
                assert_eq!(conversation["metadata"]["rev"], 1);
                assert_eq!(snapshot["syncSequence"], 1);
                assert_eq!(outbox_sequence, 1);
                assert_eq!(activity, json!({"candidate_count": 1, "counts": [1, 0]}));
            }
            2 => {
                assert_eq!(turn["projection"]["content"], "after");
                assert_eq!(conversation["metadata"]["rev"], 2);
                assert_eq!(snapshot["syncSequence"], 2);
                assert_eq!(outbox_sequence, 2);
                assert_eq!(activity, json!({"candidate_count": 1, "counts": [0, 1]}));
            }
            revision => panic!("unexpected recovered projection revision {revision}"),
        }
    }

    fn seeded_branch_parent_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "fault-branch-conversation",
                    "user_id": 11,
                    "command_id": "fault-branch-parent-append",
                    "actor": "human",
                    "projection": {"content": "parent"},
                    "turn_id": "fault-branch-parent",
                    "conversation_defaults": {
                        "allowCreate": true,
                        "title": "Fault Branch",
                        "settings": {}
                    }
                }),
                Some("fault-branch-parent-append"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn branch_create_fault_request() -> Request {
        request(
            "turn.branch.create",
            11,
            json!({
                "conversation_id": "fault-branch-conversation",
                "user_id": 11,
                "parent_turn_id": "fault-branch-parent",
                "expected_projection_revision": 1,
                "title": "Fault lane"
            }),
            Some("fault-branch-create"),
        )
    }

    fn assert_branch_create_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let parent = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-branch-conversation",
                    "user_id": 11,
                    "turn_id": "fault-branch-parent"
                }),
                None,
            ),
            3_000,
        )
        .unwrap();
        let conversation = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-branch-conversation", "user_id": 11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        let descriptors = parent["projection"]["_branchLanes"]
            .as_array()
            .map(Vec::len)
            .unwrap_or(0);
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        match parent["projectionRevision"].as_u64().unwrap() {
            1 => {
                assert_eq!(descriptors, 0);
                assert_eq!(conversation["metadata"]["rev"], 1);
                assert_eq!(outbox_sequence, 1);
            }
            2 => {
                assert_eq!(descriptors, 1);
                assert_eq!(conversation["metadata"]["rev"], 2);
                assert_eq!(outbox_sequence, 2);
            }
            revision => panic!("unexpected recovered branch-create revision {revision}"),
        }
    }

    fn seeded_branch_delete_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String) {
        let mut database = seeded_branch_parent_database(vfs.clone());
        let created = execute(&mut database, &branch_create_fault_request(), 2_000).unwrap();
        let lane_id = created["lane"]["laneId"].as_str().unwrap().to_owned();
        execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "fault-branch-conversation",
                    "user_id": 11,
                    "command_id": "fault-branch-child-append",
                    "actor": "assistant",
                    "lane_id": lane_id,
                    "projection": {"content": "child"},
                    "turn_id": "fault-branch-child"
                }),
                Some("fault-branch-child-append"),
            ),
            3_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, lane_id)
    }

    fn branch_delete_fault_request(lane_id: &str) -> Request {
        request(
            "turn.branch.delete",
            11,
            json!({
                "conversation_id": "fault-branch-conversation",
                "user_id": 11,
                "parent_turn_id": "fault-branch-parent",
                "lane_id": lane_id
            }),
            Some("fault-branch-delete"),
        )
    }

    fn assert_branch_delete_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        lane_id: &str,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let parent = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-branch-conversation",
                    "user_id": 11,
                    "turn_id": "fault-branch-parent"
                }),
                None,
            ),
            4_000,
        )
        .unwrap();
        let child = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-branch-conversation",
                    "user_id": 11,
                    "turn_id": "fault-branch-child"
                }),
                None,
            ),
            4_001,
        )
        .unwrap();
        let lane = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({
                    "conversation_id": "fault-branch-conversation",
                    "user_id": 11,
                    "lane_id": lane_id
                }),
                None,
            ),
            4_002,
        )
        .unwrap();
        let conversation = execute(
            &mut recovered,
            &request(
                "conversation.get",
                11,
                json!({"conv_id": "fault-branch-conversation", "user_id": 11}),
                None,
            ),
            4_003,
        )
        .unwrap();
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        match parent["projectionRevision"].as_u64().unwrap() {
            2 => {
                assert_eq!(
                    parent["projection"]["_branchLanes"]
                        .as_array()
                        .unwrap()
                        .len(),
                    1
                );
                assert_eq!(child["turnId"], "fault-branch-child");
                assert_eq!(lane.as_array().unwrap().len(), 1);
                assert_eq!(conversation["metadata"]["rev"], 3);
                assert_eq!(outbox_sequence, 3);
            }
            3 => {
                assert_eq!(parent["projection"]["_branchLanes"], json!([]));
                assert!(child.is_null());
                assert_eq!(lane, json!([]));
                assert_eq!(conversation["metadata"]["rev"], 4);
                assert_eq!(outbox_sequence, 4);
            }
            revision => panic!("unexpected recovered branch-delete revision {revision}"),
        }
    }

    fn seeded_attempt_claim_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: "fault-claim-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: br#"{"content":""}"#.to_vec(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "fault-claim-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "fault-claim-turn".to_owned(),
                attempt_id: Some("fault-claim-attempt".to_owned()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Fault claim".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_attempt_claim_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!(true)
        );
        let attempt = execute(
            &mut recovered,
            &request(
                "turn.attempt.get",
                11,
                json!({"attempt_id": "fault-claim-attempt", "user_id": 11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(
            attempt["taskId"],
            "@dispatching:fault-claim-attempt:fault-worker"
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    fn attempt_claim_command() -> Request {
        request(
            "turn.attempt.claim",
            11,
            json!({
                "attempt_id": "fault-claim-attempt",
                "user_id": 11,
                "dispatch_owner_id": "fault-worker"
            }),
            Some("fault-claim-command"),
        )
    }

    fn attempt_create_command() -> Request {
        request(
            "turn.attempt.create",
            11,
            json!({
                "conversation_id": "fault-create-conversation",
                "turn_id": "fault-create-turn",
                "user_id": 11,
                "command_id": "fault-create-idempotency",
                "operation": "regenerate",
                "dispatch_mode": "conversation_executor",
                "target_actor": "assistant",
                "target_kind": "reply",
                "expected_projection_revision": 1,
                "config": {}
            }),
            Some("fault-create-command"),
        )
    }

    fn seeded_attempt_create_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: "fault-create-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json:
                    br#"{"content":"done","thinking":"trace","segments":[],"toolRounds":[]}"#
                        .to_vec(),
                projection_timestamp: None,
                settlement_json: br#"{"outcome":"completed","resumeOptions":[]}"#.to_vec(),
                lane_id: "main".to_owned(),
                command_id: "fault-create-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "fault-create-turn".to_owned(),
                attempt_id: Some("fault-create-old-attempt".to_owned()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Fault create".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_attempt_create_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let result = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(result["turn"]["status"], "pending");
        assert_eq!(result["turn"]["projectionRevision"], 2);
        assert_eq!(result["attempt"]["operation"], "regenerate");
        assert_eq!(result["streamCursor"], 1);
        let events = execute(
            &mut recovered,
            &request(
                "turn.events.list",
                11,
                json!({
                    "attempt_id": result["attempt"]["attemptId"],
                    "user_id": 11,
                    "projection_mode": "patch"
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(events.as_array().unwrap().len(), 1);
        assert_eq!(events[0]["type"], "status_changed");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    fn attempt_bind_command() -> Request {
        request(
            "turn.attempt.bind",
            11,
            json!({
                "attempt_id": "fault-claim-attempt",
                "task_id": "fault-task",
                "user_id": 11,
                "dispatch_owner_id": "fault-worker"
            }),
            Some("fault-bind-command"),
        )
    }

    fn attempt_start_command() -> Request {
        request(
            "turn.attempt.start",
            11,
            json!({
                "attempt_id": "fault-claim-attempt",
                "task_id": "fault-task",
                "user_id": 11
            }),
            Some("fault-start-command"),
        )
    }

    fn seeded_attempt_bind_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = seeded_attempt_claim_database(vfs.clone());
        execute(&mut database, &attempt_claim_command(), 1_100).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn attempt_dispatch_worker_command() -> Request {
        request(
            "turn.attempt.dispatch_worker",
            11,
            json!({
                "attempt_id": deterministic_attempt_id_for_test(
                    "fault-create-conversation",
                    "fault-create-turn",
                    "fault-create-idempotency"
                ),
                "user_id": 11,
                "principal": {
                    "kind": "user",
                    "subject_id": "fault-user",
                    "owner_user_id": 11,
                    "tenant_id": null,
                    "scopes": []
                },
                "now_ms": 2_000
            }),
            Some("fault-dispatch-worker-command"),
        )
    }

    fn deterministic_attempt_id_for_test(
        conversation_id: &str,
        turn_id: &str,
        command_id: &str,
    ) -> String {
        let request = request(
            "turn.attempt.create",
            11,
            json!({
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "user_id": 11,
                "command_id": command_id,
                "operation": "continue",
                "expected_projection_revision": 1
            }),
            Some("attempt-id-probe"),
        );
        let negotiated = NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        };
        let scope = AuthenticatedScope {
            owner_id: 11,
            tenant_id: Some(7),
        };
        let admitted = admit_request(&request, negotiated, scope, 1).unwrap();
        deterministic_attempt_id(admitted, conversation_id, turn_id, command_id).unwrap()
    }

    fn seeded_attempt_dispatch_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = seeded_attempt_create_database(vfs.clone());
        execute(&mut database, &attempt_create_command(), 1_100).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_attempt_dispatch_replay_converges(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let command = attempt_dispatch_worker_command();
        let dispatched = execute(&mut recovered, &command, 3_000).unwrap();
        let attempt_id = command.payload.as_slice();
        let attempt_id = serde_json::from_slice::<Value>(attempt_id).unwrap()["attempt_id"]
            .as_str()
            .unwrap()
            .to_owned();
        let task_id = format!("conversation-attempt:{attempt_id}");
        assert_eq!(dispatched["attempt"]["taskId"], task_id);
        assert_eq!(dispatched["job"]["taskId"], task_id);
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "worker_job.get",
                    11,
                    json!({"task_id": task_id, "user_id": 11}),
                    None,
                ),
                3_001,
            )
            .unwrap()["payload"]["attemptId"],
            attempt_id
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "turn.attempt.dispatchable.list",
                    99,
                    json!({"created_before_ms": 10_000}),
                    None,
                ),
                3_002,
            )
            .unwrap(),
            json!([])
        );
    }

    fn seeded_attempt_start_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = seeded_attempt_bind_database(vfs.clone());
        execute(&mut database, &attempt_bind_command(), 1_200).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_attempt_lifecycle_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
        expected_status: &str,
        expected_projection_revision: u64,
        expected_sequence: u64,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let attempt = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(attempt["taskId"], "fault-task");
        assert_eq!(attempt["status"], expected_status);
        let turn = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-claim-conversation",
                    "turn_id": "fault-claim-turn",
                    "user_id": 11
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(turn["status"], expected_status);
        assert_eq!(turn["projectionRevision"], expected_projection_revision);
        let timing_list = execute(
            &mut recovered,
            &request(
                "turn.timing_trace.list",
                11,
                json!({
                    "conversation_id": "fault-claim-conversation",
                    "user_id": 11,
                    "limit": 10
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(timing_list["records"].as_array().unwrap().len(), 1);
        assert_eq!(timing_list["records"][0]["task_id"], "fault-task");
        assert_eq!(timing_list["records"][0]["status"], expected_status);
        let changes = execute(
            &mut recovered,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "fault-claim-conversation",
                    "user_id": 11,
                    "after": expected_sequence - 1
                }),
                None,
            ),
            3_002,
        )
        .unwrap();
        assert_eq!(changes["head"], expected_sequence);
        assert_eq!(changes["events"][0]["type"], "attempt.event");
        assert_eq!(changes["events"][0]["attemptId"], "fault-claim-attempt");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            expected_sequence
        );
    }

    fn certify_attempt_lifecycle_faults(
        seed: fn(std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase,
        command: &Request,
        expected_status: &str,
        expected_projection_revision: u64,
        expected_sequence: u64,
    ) {
        let baseline_vfs = prepared_vfs();
        let mut baseline = seed(baseline_vfs.clone());
        execute(&mut baseline, command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seed(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, command, 2_000);
            drop(database);
            assert_attempt_lifecycle_replay_converges(
                vfs,
                command,
                expected_status,
                expected_projection_revision,
                expected_sequence,
            );
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seed(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, command, 2_000);
            drop(database);
            assert_attempt_lifecycle_replay_converges(
                vfs,
                command,
                expected_status,
                expected_projection_revision,
                expected_sequence,
            );
        }
    }

    fn certify_attempt_dispatch_faults() {
        let command = attempt_dispatch_worker_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_attempt_dispatch_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_dispatch_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_attempt_dispatch_replay_converges(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_dispatch_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_attempt_dispatch_replay_converges(vfs);
        }
    }

    fn perception_record_command() -> Request {
        request(
            "turn.perception.record",
            11,
            json!({
                "conversation_id": "fault-claim-conversation",
                "turn_id": "fault-claim-turn",
                "attempt_id": "fault-claim-attempt",
                "user_id": 11,
                "observation": {
                    "observationId": "paint:terminal:1",
                    "attemptId": "fault-claim-attempt",
                    "kind": "terminal_painted",
                    "clientId": "fault-page",
                    "serverEmittedAt": 1_900,
                    "receivedAt": 2_025,
                    "paintedAt": 2_060,
                    "projectionRevision": 3,
                    "visibility": "visible"
                }
            }),
            Some("fault-perception-command"),
        )
    }

    fn assert_perception_record_replay_converges(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, &perception_record_command(), 3_000).unwrap();
        assert_eq!(replay["conversationRevision"], 2);
        assert_eq!(replay["projectionRevision"], 2);
        let trace = execute(
            &mut recovered,
            &request(
                "turn.timing_trace.get",
                11,
                json!({"task_id": "fault-task", "user_id": 11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        let observations = trace["timingTrace"]["clientObservations"]
            .as_array()
            .unwrap();
        assert_eq!(observations.len(), 1);
        assert_eq!(observations[0]["observationId"], "paint:terminal:1");
        assert_eq!(observations[0]["renderMs"], 35);
        assert_eq!(observations[0]["transportMs"], 125);
        let idempotent = execute(&mut recovered, &perception_record_command(), 3_100).unwrap();
        assert_eq!(idempotent["applied"], false);
        assert_eq!(idempotent["idempotentReplay"], true);
        let sync = execute(
            &mut recovered,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "fault-claim-conversation",
                    "user_id": 11,
                    "after": 0
                }),
                None,
            ),
            3_101,
        )
        .unwrap();
        assert_eq!(sync["head"], 2);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_perception_record_commit_fault_recovers_one_receipt_without_sync_or_outbox_churn() {
        let command = perception_record_command();
        let command_payload: Value = serde_json::from_slice(&command.payload).unwrap();
        assert!(crate::turn::perception_observation_is_valid(
            command_payload["observation"].as_object().unwrap(),
            "fault-claim-attempt",
        ));
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_attempt_start_database(baseline_vfs.clone());
        let first = execute(&mut baseline, &command, 2_000).unwrap();
        assert_eq!(first["applied"], true);
        assert!(first.get("idempotentReplay").is_none());
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_start_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_perception_record_replay_converges(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_start_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_perception_record_replay_converges(vfs);
        }
    }

    #[test]
    fn every_attempt_worker_dispatch_commit_fault_recovers_one_atomic_prefix() {
        certify_attempt_dispatch_faults();
    }

    #[test]
    fn every_attempt_bind_commit_fault_recovers_one_bound_prefix() {
        certify_attempt_lifecycle_faults(
            seeded_attempt_bind_database,
            &attempt_bind_command(),
            "pending",
            2,
            2,
        );
    }

    #[test]
    fn every_attempt_create_commit_fault_recovers_one_complete_attempt_prefix() {
        let command = attempt_create_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_attempt_create_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_create_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_attempt_create_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_create_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_attempt_create_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_attempt_start_commit_fault_recovers_one_running_prefix() {
        certify_attempt_lifecycle_faults(
            seeded_attempt_start_database,
            &attempt_start_command(),
            "running",
            3,
            3,
        );
    }

    #[test]
    fn every_attempt_claim_commit_fault_recovers_one_claimed_prefix() {
        let command = attempt_claim_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_attempt_claim_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_claim_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_attempt_claim_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_attempt_claim_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_attempt_claim_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_settled_turn_commit_fault_recovers_one_complete_transcript_prefix() {
        let command = request(
            "turn.append_settled",
            11,
            json!({
                "conversation_id": "fault-turn-conversation",
                "user_id": 11,
                "command_id": "fault-turn-ingest",
                "actor": "assistant",
                "projection": {"content": "x".repeat(20_000)},
                "turn_id": "fault-turn",
                "conversation_defaults": {
                    "allowCreate": true,
                    "title": "Fault Turn",
                    "settings": {"model": "fault-model"}
                }
            }),
            Some("fault-turn-receipt"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_settled_turn_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_settled_turn_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_turn_delete_commit_fault_recovers_one_complete_state_prefix() {
        let delete = request(
            "turn.delete",
            11,
            json!({
                "conversation_id": "fault-delete-conversation",
                "user_id": 11,
                "turn_ids": ["fault-delete-turn"]
            }),
            Some("fault-delete-command"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_turn_delete_database(baseline_vfs.clone());
        execute(&mut baseline, &delete, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_turn_delete_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &delete, 2_000);
            drop(database);
            assert_turn_delete_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_turn_delete_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &delete, 2_000);
            drop(database);
            assert_turn_delete_recovers_atomic_prefix(vfs);
        }
    }

    #[test]
    fn every_turn_compaction_commit_fault_recovers_one_complete_graph() {
        let command = turn_compaction_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_turn_compaction_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_turn_compaction_replay_converges(baseline_vfs, &command);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_turn_compaction_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_turn_compaction_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_turn_compaction_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_turn_compaction_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn every_projection_update_commit_fault_recovers_one_complete_state_prefix() {
        let update = request(
            "turn.projection.update",
            11,
            json!({
                "conversation_id": "fault-edit-conversation",
                "user_id": 11,
                "turn_id": "fault-edit-turn",
                "expected_projection_revision": 1,
                "projection": {
                    "content": "after", "toolRounds": [], "timestamp": 200
                }
            }),
            Some("fault-edit-update"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_projection_update_database(baseline_vfs.clone());
        execute(&mut baseline, &update, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_projection_update_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &update, 2_000);
            drop(database);
            assert_projection_update_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_projection_update_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &update, 2_000);
            drop(database);
            assert_projection_update_recovers_atomic_prefix(vfs);
        }
    }

    #[test]
    fn every_branch_create_commit_fault_recovers_one_complete_state_prefix() {
        let command = branch_create_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_branch_parent_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_branch_parent_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_branch_create_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_branch_parent_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_branch_create_recovers_atomic_prefix(vfs);
        }
    }

    #[test]
    fn every_branch_delete_commit_fault_recovers_one_complete_state_prefix() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, lane_id) = seeded_branch_delete_database(baseline_vfs.clone());
        let command = branch_delete_fault_request(&lane_id);
        execute(&mut baseline, &command, 4_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, seeded_lane_id) = seeded_branch_delete_database(vfs.clone());
            assert_eq!(seeded_lane_id, lane_id);
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_branch_delete_recovers_atomic_prefix(vfs, &lane_id);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, seeded_lane_id) = seeded_branch_delete_database(vfs.clone());
            assert_eq!(seeded_lane_id, lane_id);
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 4_000);
            drop(database);
            assert_branch_delete_recovers_atomic_prefix(vfs, &lane_id);
        }
    }

    fn assert_semantic_replay_converges(vfs: std::sync::Arc<DeterministicVfs>, command: &Request) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert!(
            replayed == present_preference("agent-1", 2_000)
                || replayed == present_preference("agent-1", 3_000)
        );
        let get = request(
            "desktop.egress_agent.get",
            11,
            json!({"owner_user_id": 11}),
            None,
        );
        assert_eq!(execute(&mut recovered, &get, 3_001).unwrap(), replayed);
        let status = recovered.logical_outbox_status(7, 11).unwrap();
        assert_eq!(status.last_sequence, 1);
        assert_eq!(status.published_sequence, 0);
    }

    #[test]
    fn every_semantic_commit_fault_converges_mutation_receipt_and_outbox_atomically() {
        let command = request(
            "desktop.egress_agent.set",
            11,
            json!({"owner_user_id": 11, "agent_id": "agent-1"}),
            Some("faulted-command"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_semantic_replay_converges(vfs, &command);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_semantic_replay_converges(vfs, &command);
        }
    }

    fn assert_record_blob_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["version"], 1);
        assert!(replayed["updated_at_ms"] == 2_000 || replayed["updated_at_ms"] == 3_000);
        let get = request(
            "record.get",
            11,
            json!({"namespace": "fault", "key": "large"}),
            None,
        );
        let stored = execute(&mut recovered, &get, 3_001).unwrap();
        assert_eq!(stored["version"], 1);
        assert_eq!(stored["updated_at_ms"], replayed["updated_at_ms"]);
        assert_eq!(stored["value"].as_str().unwrap().len(), 20_000);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_record_blob_commit_fault_recovers_one_atomic_semantic_version() {
        let command = request(
            "record.put",
            11,
            json!({"namespace": "fault", "key": "large", "value": "x".repeat(20_000)}),
            Some("faulted-record-blob"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_record_blob_replay_converges(vfs, &command);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_record_blob_replay_converges(vfs, &command);
        }
    }

    fn assert_task_result_checkpoint_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["version"], 1);
        assert!(replayed["updated_at_ms"] == 2_000 || replayed["updated_at_ms"] == 3_000);

        let mut advanced_payload: Value = serde_json::from_slice(&command.payload).unwrap();
        advanced_payload["value"]["status"] = json!("running");
        advanced_payload["expected_version"] = json!(1);
        let advanced = request(
            "task_results.checkpoint",
            11,
            advanced_payload,
            Some("faulted-task-result-advance"),
        );
        assert_eq!(
            execute(&mut recovered, &advanced, 3_001).unwrap()["version"],
            2
        );
    }

    #[test]
    fn every_task_result_blob_commit_fault_recovers_one_atomic_checkpoint() {
        let command = request(
            "task_results.checkpoint",
            11,
            json!({
                "key": "fault-task-result",
                "value": {
                    "user_id": 11,
                    "task_id": "fault-task-result",
                    "status": "pending",
                    "content": "x".repeat(20_000),
                },
                "expected_version": 0,
            }),
            Some("faulted-task-result"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_task_result_checkpoint_replay_converges(vfs, &command);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_task_result_checkpoint_replay_converges(vfs, &command);
        }
    }

    fn seeded_guarded_task_result_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs);
        let create = request(
            "conversation.create",
            11,
            json!({
                "conv_id": "guarded-fault-conversation",
                "user_id": 11,
                "title": "Guarded",
                "settings": {"unrelated": "preserved"},
                "created_at": 500,
                "updated_at": 500,
            }),
            Some("seed-guarded-conversation"),
        );
        execute(&mut database, &create, 500).unwrap();
        database
    }

    fn guarded_task_result_command() -> Request {
        request(
            "task_results.checkpoint",
            11,
            json!({
                "key": "guarded-fault-task",
                "value": {
                    "user_id": 11,
                    "task_id": "guarded-fault-task",
                    "conv_id": "guarded-fault-conversation",
                    "status": "running",
                    "cache_prefix_hwm": 19,
                    "last_turn_cache_read": 3_400,
                    "content": "x".repeat(20_000),
                },
                "expected_version": 0,
                "guard_contract": TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,
                "require_parent": true,
                "cache_settings_contract": TASK_RESULT_CACHE_SETTINGS_CONTRACT,
            }),
            Some("faulted-guarded-task-result"),
        )
    }

    fn assert_guarded_task_result_replay_converges(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let result = execute(&mut recovered, &guarded_task_result_command(), 3_000).unwrap();
        assert_eq!(result["owned"], true);
        assert_eq!(result["version"], 1);
        assert_eq!(result["cache_settings_committed"], true);
        assert_eq!(result["cache_prefix_hwm"], 19);
        assert_eq!(result["last_turn_cache_read"], 3_400);
        let get = request(
            "conversation.get",
            11,
            json!({
                "conv_id": "guarded-fault-conversation",
                "user_id": 11,
                "derive_messages": false,
            }),
            None,
        );
        let parent = execute(&mut recovered, &get, 3_001).unwrap();
        assert_eq!(parent["metadata"]["settings"]["cachePrefixHWM"], 19);
        assert_eq!(parent["metadata"]["settings"]["lastTurnCacheRead"], 3_400);
        assert_eq!(parent["metadata"]["settings"]["unrelated"], "preserved");
    }

    #[test]
    fn every_guarded_task_result_fault_keeps_parent_facts_and_checkpoint_atomic() {
        let command = guarded_task_result_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_guarded_task_result_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_guarded_task_result_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_guarded_task_result_replay_converges(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_guarded_task_result_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_guarded_task_result_replay_converges(vfs);
        }
    }

    fn seeded_task_result_abort_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs);
        let checkpoint = request(
            "task_results.checkpoint",
            11,
            json!({
                "key": "fault-task-abort",
                "value": {
                    "user_id": 11,
                    "task_id": "fault-task-abort",
                    "conv_id": "fault-conversation",
                    "status": "running",
                    "content": "x".repeat(20_000),
                },
                "expected_version": 0,
            }),
            Some("seed-task-result-abort"),
        );
        execute(&mut database, &checkpoint, 1_000).unwrap();
        database
    }

    fn assert_task_result_abort_replay_converges(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let requested = request(
            "task_results.abort_requested",
            11,
            json!({"task_id": "fault-task-abort", "user_id": 11}),
            None,
        );
        let before = execute(&mut recovered, &requested, 3_000).unwrap()["requested"]
            .as_bool()
            .unwrap();
        let abort = request(
            "task_results.abort",
            11,
            json!({"task_id": "fault-task-abort", "user_id": 11, "source": "fault"}),
            Some("fault-task-result-abort"),
        );
        let replay = execute(&mut recovered, &abort, 3_001).unwrap();
        assert_eq!(replay["signaled"], true);
        assert_eq!(replay["changed"], !before);
        assert_eq!(
            execute(&mut recovered, &requested, 3_002).unwrap()["requested"],
            true
        );
        let summary = request(
            "task_results.summary_list",
            11,
            json!({"user_id": 11, "conv_id": "fault-conversation"}),
            None,
        );
        let listed = execute(&mut recovered, &summary, 3_003).unwrap();
        assert_eq!(listed["records"][0]["version"], 2);
    }

    #[test]
    fn every_task_result_abort_fault_recovers_header_and_summary_together() {
        let command = request(
            "task_results.abort",
            11,
            json!({"task_id": "fault-task-abort", "user_id": 11, "source": "fault"}),
            Some("fault-task-result-abort"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_task_result_abort_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_task_result_abort_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_task_result_abort_replay_converges(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_task_result_abort_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_task_result_abort_replay_converges(vfs);
        }
    }

    fn assert_task_result_recovery_replay_converges(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let recover = request(
            "task_results.recover_running",
            11,
            json!({"interrupted_reason": "server_restart", "max_rows": 32,
                   "scan_limit": 10_000}),
            Some("fault-task-result-recover"),
        );
        execute(&mut recovered, &recover, 3_000).unwrap();
        let replay = request(
            "task_results.replay_get",
            11,
            json!({"key": "fault-task-abort", "user_id": 11}),
            None,
        );
        let value = execute(&mut recovered, &replay, 3_001).unwrap();
        assert_eq!(value["status"], "interrupted");
        assert_eq!(value["version"], 2);
        let second = execute(&mut recovered, &recover, 3_002).unwrap();
        assert_eq!(second["recovered"], json!([]));
    }

    #[test]
    fn every_task_result_recovery_fault_recovers_header_summary_and_live_index_together() {
        let command = request(
            "task_results.recover_running",
            11,
            json!({"interrupted_reason": "server_restart", "max_rows": 32,
                   "scan_limit": 10_000}),
            Some("fault-task-result-recover"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_task_result_abort_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_task_result_abort_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_task_result_recovery_replay_converges(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_task_result_abort_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_task_result_recovery_replay_converges(vfs);
        }
    }

    fn assert_indexed_event_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert!(replay["inserted"] == true || replay["inserted"] == false);
        let bounds = request("event.bounds", 11, json!({"task_id": "fault-event"}), None);
        assert_eq!(
            execute(&mut recovered, &bounds, 3_001).unwrap(),
            json!({"retained_count": 1, "base_cursor": 19, "next_cursor": 20})
        );
        let list = request(
            "event.list",
            11,
            json!({"task_id": "fault-event", "types": ["delta"]}),
            None,
        );
        let rows = execute(&mut recovered, &list, 3_002).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["sequence"], 19);
        assert_eq!(
            rows[0]["event"],
            json!({"type": "delta", "text": "durable",
                "v": crate::generated_tofudb_ir::EVENT_FORMAT_VERSION})
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_indexed_event_commit_fault_recovers_one_atomic_natural_key() {
        let command = request(
            "event.append",
            11,
            json!({"task_id": "fault-event", "sequence": 19, "event": {"type": "delta", "text": "durable"}}),
            Some("faulted-indexed-event"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_indexed_event_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_indexed_event_replay_converges(vfs, &command);
        }
    }

    fn seeded_retention_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        for (sequence, event, timestamp) in [
            (10, json!({"type": "delta", "text": "expired"}), 1_000),
            (
                20,
                json!({"type": "messages_snapshot", "kind": "request"}),
                1_000,
            ),
            (30, json!({"type": "delta", "text": "retained"}), 2_000),
        ] {
            let append = request(
                "event.append",
                11,
                json!({"task_id": "fault-prune", "sequence": sequence, "event": event}),
                Some(&format!("fault-prune-seed-{sequence}")),
            );
            execute(&mut database, &append, timestamp).unwrap();
        }
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_event_prune_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert!(replay["deleted"] == 0 || replay["deleted"] == 1);
        let bounds = request("event.bounds", 11, json!({"task_id": "fault-prune"}), None);
        assert_eq!(
            execute(&mut recovered, &bounds, 3_001).unwrap(),
            json!({"retained_count": 2, "base_cursor": 20, "next_cursor": 31})
        );
        let physical = crate::stream::StreamKey::new(7, 11, "task_event", b"fault-prune").unwrap();
        assert_eq!(
            recovered
                .stream_read(7, 11, &physical, 1, 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
        assert_eq!(
            recovered
                .stream_read(7, 11, &physical, 2, 1)
                .unwrap()
                .events[0]
                .sequence,
            2
        );
        let deltas = request(
            "event.list",
            11,
            json!({"task_id": "fault-prune", "types": ["delta"]}),
            None,
        );
        let rows = execute(&mut recovered, &deltas, 3_002).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["sequence"], 30);
        let summary = request(
            "event.inspector_summary",
            11,
            json!({"task_ids": ["fault-prune"]}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &summary, 3_003).unwrap()["records"][0]["request_count"],
            1
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            4
        );
    }

    #[test]
    fn every_event_prune_commit_fault_recovers_all_indexes_atomically() {
        let command = request(
            "event.prune",
            11,
            json!({
                "created_before_ms": 1_500,
                "retention_class": "streaming",
                "limit": 100
            }),
            Some("faulted-event-prune"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_retention_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_500).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_retention_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_500);
            drop(database);
            assert_event_prune_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_retention_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_500);
            drop(database);
            assert_event_prune_replay_converges(vfs, &command);
        }
    }

    fn assert_event_batch_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert!(replay["inserted"] == 0 || replay["inserted"] == 3);
        assert!(replay["deduplicated"] == 0 || replay["deduplicated"] == 3);
        assert_ne!(replay["inserted"], replay["deduplicated"]);
        for (task_id, count, base, next) in
            [("fault-batch-a", 2, 4, 10), ("fault-batch-b", 1, 7, 8)]
        {
            let bounds = request("event.bounds", 11, json!({"task_id": task_id}), None);
            assert_eq!(
                execute(&mut recovered, &bounds, 3_001).unwrap(),
                json!({"retained_count": count, "base_cursor": base, "next_cursor": next})
            );
        }
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_event_batch_commit_fault_recovers_all_streams_atomically() {
        let command = request(
            "event.append_batch",
            11,
            json!({"events": [
                {"task_id": "fault-batch-a", "sequence": 4, "event": {"value": "first"}},
                {"task_id": "fault-batch-b", "sequence": 7, "event": {"value": "second"}},
                {"task_id": "fault-batch-a", "sequence": 9, "event": {"value": "third"}}
            ]}),
            Some("faulted-event-batch"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_event_batch_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_event_batch_replay_converges(vfs, &command);
        }
    }

    fn tenant_user_fault_command() -> Request {
        request(
            "tenant.user.create",
            1,
            json!({
                "user_id": "fault-account", "email": "FAULT@EXAMPLE.COM",
                "password_hash": "sealed", "display_name": "Fault", "role": "admin",
                "created_at": 100, "metadata": {"payload": "x".repeat(20_000)},
            }),
            Some("fault-tenant-user-create"),
        )
    }

    fn assert_tenant_user_create_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["id"], "fault-account");
        assert_eq!(replayed["owner_user_id"], 2);
        assert!(replayed.get("password_hash").is_none());
        let authentication = request(
            "tenant.user.authentication",
            1,
            json!({"email": "fault@example.com"}),
            None,
        );
        let authenticated = execute(&mut recovered, &authentication, 3_001).unwrap();
        assert_eq!(authenticated["password_hash"], "sealed");
        assert_eq!(authenticated["user"], replayed);
    }

    #[test]
    fn every_tenant_user_create_fault_recovers_profile_indexes_sequence_and_receipt_atomically() {
        let command = tenant_user_fault_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_tenant_user_create_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_tenant_user_create_recovers_atomic_prefix(vfs, &command);
        }
    }

    fn credential_create_command(credential_id: &str, expires_at: Value) -> Request {
        let digest = blake3::hash(credential_id.as_bytes());
        let secret_hash: String = digest
            .as_bytes()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        let mut payload = json!({
            "credential_id": credential_id, "owner_user_id": 11,
            "tenant_id": "personal", "account_user_id": "",
            "name": "Expiry credential", "prefix": "tf_exp_",
            "secret_hash": secret_hash, "scopes": ["read"],
            "rate_limit_rpm": 60, "rate_limit_tpd": 1000,
            "created_at": 100, "metadata": {},
        });
        payload
            .as_object_mut()
            .unwrap()
            .insert("expires_at".to_owned(), expires_at);
        request("credential.create", 11, payload, Some(credential_id))
    }

    #[test]
    fn credential_create_treats_explicit_null_expires_at_as_absent() {
        let mut database = simulated_database(prepared_vfs());
        // Explicit JSON null must behave identically to omitting expires_at.
        let null_create = credential_create_command("cred-null", Value::Null);
        let null_result = execute(&mut database, &null_create, 1_000).unwrap();
        assert_eq!(null_result["id"], "cred-null");
        assert!(null_result.get("expires_at").is_none() || null_result["expires_at"].is_null());
        // A valid numeric expiry is accepted.
        let numeric_create = credential_create_command("cred-numeric", json!(4_000));
        let numeric_result = execute(&mut database, &numeric_create, 1_001).unwrap();
        assert_eq!(numeric_result["expires_at"], json!(4_000.0));
        // A wrong-typed expiry is still rejected.
        let bad_create = credential_create_command("cred-bad", json!("abc"));
        assert!(matches!(
            execute(&mut database, &bad_create, 1_002),
            Err(SemanticExecutionError::InvalidPayload)
        ));
    }
    fn credential_fault_command() -> Request {
        request(
            "credential.create",
            11,
            json!({
                "credential_id": "fault-credential", "owner_user_id": 11,
                "tenant_id": "personal", "account_user_id": "",
                "name": "Fault credential", "prefix": "tf_fault_",
                "secret_hash": "b".repeat(64), "scopes": ["read", "write"],
                "rate_limit_rpm": 60, "rate_limit_tpd": 1000,
                "created_at": 100, "metadata": {"payload": "x".repeat(20_000)},
            }),
            Some("fault-credential-create"),
        )
    }

    fn assert_credential_create_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["id"], "fault-credential");
        assert!(replayed.get("secret_hash").is_none());
        let validated = request(
            "credential.validate",
            11,
            json!({"secret_hash": "b".repeat(64), "now": 200}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &validated, 3_001).unwrap(),
            replayed
        );
        let listed = request(
            "credential.list",
            11,
            json!({"owner_user_id": 11, "tenant_id": "personal"}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &listed, 3_002).unwrap(),
            json!([replayed])
        );
    }

    #[test]
    fn every_credential_create_fault_recovers_profile_indexes_count_and_receipt_atomically() {
        let command = credential_fault_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_credential_create_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_credential_create_recovers_atomic_prefix(vfs, &command);
        }
    }

    fn billing_fault_command() -> Request {
        request(
            "billing.wallet.apply",
            11,
            json!({
                "user_id":"fault-billing", "amount_micro":10000,
                "kind":"topup", "ref_type":"payment", "ref_id":"fault-payment",
                "note":"durable credit", "ledger_id":"fault-ledger",
                "occurred_at":100, "allow_negative":false,
            }),
            Some("fault-billing-wallet-apply"),
        )
    }

    fn assert_billing_apply_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replay["wallet"]["balance_micro"], 10_000);
        let wallet = request(
            "billing.wallet.get",
            11,
            json!({"user_id":"fault-billing"}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &wallet, 3_001).unwrap()["balance_micro"],
            10_000
        );
        let recompute = request(
            "billing.ledger.recompute",
            11,
            json!({"user_id":"fault-billing"}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &recompute, 3_002).unwrap(),
            json!({"balance_micro":10000})
        );
        let list = request(
            "billing.ledger.list",
            11,
            json!({"user_id":"fault-billing","limit":10,"offset":0,"kinds":[]}),
            None,
        );
        let rows = execute(&mut recovered, &list, 3_003).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["id"], "fault-ledger");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_billing_wallet_apply_fault_recovers_one_complete_money_prefix() {
        let command = billing_fault_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_billing_apply_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_billing_apply_recovers_atomic_prefix(vfs, &command);
        }
    }

    fn billing_payment_record_command() -> Request {
        request(
            "billing.payment.record",
            11,
            json!({
                "id":"fault-payment-record", "user_id":"fault-payment-user",
                "provider":"stripe", "provider_id":"fault-provider-payment",
                "amount_minor":499, "currency":"USD", "credit_micro":500,
                "status":"pending", "raw":{"phase":"created"},
            }),
            Some("fault-payment-record-command"),
        )
    }

    fn billing_payment_settle_command() -> Request {
        request(
            "billing.payment.settle",
            11,
            json!({
                "payment_id":"fault-payment-record", "ledger_id":"fault-payment-ledger",
                "raw":{"phase":"settled"},
            }),
            Some("fault-payment-settle-command"),
        )
    }

    fn prepared_billing_payment_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs);
        execute(&mut database, &billing_payment_record_command(), 2_000).unwrap();
        database
    }

    fn assert_billing_payment_settle_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replay = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replay["settled"], true);
        assert_eq!(replay["payment"]["status"], "settled");
        let wallet = request(
            "billing.wallet.get",
            11,
            json!({"user_id":"fault-payment-user"}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &wallet, 3_001).unwrap()["balance_micro"],
            500
        );
        let payment = request(
            "billing.payment.find",
            11,
            json!({"provider":"stripe","provider_id":"fault-provider-payment"}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &payment, 3_002).unwrap()["status"],
            "settled"
        );
        let ledger = request(
            "billing.ledger.list",
            11,
            json!({"user_id":"fault-payment-user","limit":10,"offset":0,"kinds":[]}),
            None,
        );
        let rows = execute(&mut recovered, &ledger, 3_003).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["id"], "fault-payment-ledger");
    }

    #[test]
    fn every_billing_payment_settle_fault_recovers_payment_wallet_and_ledger_atomically() {
        let command = billing_payment_settle_command();
        let baseline_vfs = prepared_vfs();
        let mut baseline = prepared_billing_payment_database(baseline_vfs.clone());
        let setup_operations = baseline_vfs.trace().unwrap().len();
        execute(&mut baseline, &command, 3_000).unwrap();
        let trace = baseline_vfs.trace().unwrap()[setup_operations..].to_vec();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = prepared_billing_payment_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_billing_payment_settle_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = prepared_billing_payment_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_billing_payment_settle_recovers_atomic_prefix(vfs, &command);
        }
    }

    #[test]
    fn redeem_mint_preserves_the_ten_thousand_code_contract_below_write_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
            .unwrap();
        let codes: Vec<String> = (0..10_000).map(|index| format!("c{index:05}")).collect();
        let mint = request(
            "billing.redeem_codes.mint",
            11,
            json!({
                "codes":codes, "amount_micro":1, "batch":"maximum-mint",
                "created_by":"budget-test", "created_at":100, "expires_at":0,
                "note":"ten thousand exact locators",
            }),
            Some("maximum-redeem-mint"),
        );
        let minted = execute(&mut database, &mint, 2_000).unwrap();
        assert_eq!(minted["created"], 10_000);
        assert_eq!(minted["codes"].as_array().unwrap().len(), 10_000);
        let listed = request(
            "billing.redeem_codes.list",
            11,
            json!({"batch":"maximum-mint","status":"all","limit":2,"offset":0}),
            None,
        );
        let rows = execute(&mut database, &listed, 2_001).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 2);
        assert_eq!(rows[0]["code"], "c00000");
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
            .unwrap();
        assert_eq!(execute(&mut reopened, &mint, 3_000).unwrap(), minted);
    }

    fn redeem_fault_mint() -> Request {
        request(
            "billing.redeem_codes.mint",
            11,
            json!({
                "codes":["fault-redeem-code","fault-redeem-code-second"], "amount_micro":75,
                "batch":"fault-redeem-batch", "created_by":"test",
                "created_at":100, "expires_at":0, "note":"atomic redeem",
            }),
            Some("fault-redeem-mint"),
        )
    }

    fn redeem_fault_apply() -> Request {
        request(
            "billing.redeem_code.apply",
            11,
            json!({
                "code":"fault-redeem-code", "user_id":"fault-redeem-user",
                "redeemed_at":101, "ledger_id":"fault-redeem-ledger",
            }),
            Some("fault-redeem-apply"),
        )
    }

    fn prepared_redeem_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs);
        execute(&mut database, &redeem_fault_mint(), 2_000).unwrap();
        database
    }

    fn assert_redeem_mint_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap()["created"],
            2
        );
        let codes = request(
            "billing.redeem_codes.list",
            11,
            json!({"batch":"fault-redeem-batch","status":"all","limit":10,"offset":0}),
            None,
        );
        let rows = execute(&mut recovered, &codes, 3_001).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 2);
        assert_eq!(rows[0]["code"], "fault-redeem-code");
        assert_eq!(rows[1]["code"], "fault-redeem-code-second");
    }

    #[test]
    fn every_redeem_mint_fault_recovers_all_directories_and_indexes_atomically() {
        let command = redeem_fault_mint();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_redeem_mint_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_redeem_mint_recovers_atomic_prefix(vfs, &command);
        }
    }

    fn assert_redeem_apply_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap()["status"],
            "redeemed"
        );
        let wallet = request(
            "billing.wallet.get",
            11,
            json!({"user_id":"fault-redeem-user"}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &wallet, 3_001).unwrap()["balance_micro"],
            75
        );
        let codes = request(
            "billing.redeem_codes.list",
            11,
            json!({"batch":"fault-redeem-batch","status":"redeemed","limit":10,"offset":0}),
            None,
        );
        let rows = execute(&mut recovered, &codes, 3_002).unwrap();
        assert_eq!(rows.as_array().unwrap().len(), 1);
        assert_eq!(rows[0]["redeemed_by"], "fault-redeem-user");
        let ledger = request(
            "billing.ledger.list",
            11,
            json!({"user_id":"fault-redeem-user","limit":10,"offset":0,"kinds":[]}),
            None,
        );
        assert_eq!(
            execute(&mut recovered, &ledger, 3_003)
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn every_redeem_apply_fault_recovers_code_wallet_and_ledger_atomically() {
        let command = redeem_fault_apply();
        let baseline_vfs = prepared_vfs();
        let mut baseline = prepared_redeem_database(baseline_vfs.clone());
        let setup_operations = baseline_vfs.trace().unwrap().len();
        execute(&mut baseline, &command, 3_000).unwrap();
        let trace = baseline_vfs.trace().unwrap()[setup_operations..].to_vec();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = prepared_redeem_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_redeem_apply_recovers_atomic_prefix(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = prepared_redeem_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 3_000);
            drop(database);
            assert_redeem_apply_recovers_atomic_prefix(vfs, &command);
        }
    }

    fn queue_fault_request() -> Request {
        request(
            "queue.enqueue",
            11,
            json!({
                "conv_id":"queue-fault-conversation", "user_id":11,
                "queue_id":"queue-fault-item", "message":{"text":"durable"},
                "config":{"model":"m"}, "kind":"real", "priority":10,
                "created_at_ms":2_000
            }),
            Some("queue-fault-enqueue"),
        )
    }

    fn swarm_fault_request() -> Request {
        request(
            "swarm.agent.save",
            11,
            json!({
                "swarm_key":"swarm-fault","agent_id":"agent-a",
                "role":"coder","objective":"recover atomically",
                "status":"completed",
                "messages":[{"role":"assistant","content":"durable result"}],
                "result":{"final_answer":"done"},"rounds_used":2,
                "delivered":false,"now_ms":2_000
            }),
            Some("swarm-fault-agent-save"),
        )
    }

    fn integration_register_request(owner_id: u64, task_id: &str, now: f64) -> Request {
        request(
            "integration.workspace.register",
            owner_id,
            json!({
                "user_id":owner_id,"project_root":"/shared-project",
                "task_id":task_id,"title":task_id,
                "workspace_path":format!("/workspace/{task_id}"),
                "managed":false,"base_sha":"a".repeat(40),"now":now
            }),
            Some(&format!("integration-register-{owner_id}-{task_id}")),
        )
    }

    fn prepare_integration_ready(
        database: &mut AuthorityDatabase,
        owner_id: u64,
        task_id: &str,
        now: f64,
    ) {
        execute(
            database,
            &integration_register_request(owner_id, task_id, now),
            now as u64,
        )
        .unwrap();
        execute(
            database,
            &request(
                "integration.workspace.save_checkpoint",
                owner_id,
                json!({
                    "user_id":owner_id,"project_root":"/shared-project",
                    "task_id":task_id,"checkpoint_sha":"b".repeat(40),
                    "now":now + 0.1
                }),
                Some(&format!("integration-checkpoint-{owner_id}-{task_id}")),
            ),
            now as u64,
        )
        .unwrap();
        execute(
            database,
            &request(
                "integration.workspace.submit",
                owner_id,
                json!({
                    "user_id":owner_id,"project_root":"/shared-project",
                    "task_id":task_id,"now":now + 0.2
                }),
                Some(&format!("integration-submit-{owner_id}-{task_id}")),
            ),
            now as u64,
        )
        .unwrap();
    }

    #[test]
    fn integration_global_claim_serializes_one_project_across_owners() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        prepare_integration_ready(&mut database, 11, "alpha", 100.0);
        prepare_integration_ready(&mut database, 12, "beta", 200.0);

        let first = execute(
            &mut database,
            &request(
                "integration.workspace.claim_next",
                11,
                json!({"now":300.0}),
                Some("integration-first-claim"),
            ),
            300,
        )
        .unwrap();
        assert_eq!(first["user_id"], 11);
        assert_eq!(first["task_id"], "alpha");
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "integration.workspace.claim_next",
                    11,
                    json!({"now":301.0}),
                    Some("integration-blocked-claim"),
                ),
                301,
            )
            .unwrap(),
            Value::Null
        );
        execute(
            &mut database,
            &request(
                "integration.workspace.mark_merged",
                11,
                json!({
                    "row_id":first["id"],"candidate_sha":"c".repeat(40),
                    "now":302.0
                }),
                Some("integration-first-merged"),
            ),
            302,
        )
        .unwrap();
        let second = execute(
            &mut database,
            &request(
                "integration.workspace.claim_next",
                11,
                json!({"now":303.0}),
                Some("integration-second-claim"),
            ),
            303,
        )
        .unwrap();
        assert_eq!(second["user_id"], 12);
        assert_eq!(second["task_id"], "beta");
    }

    #[test]
    fn integration_events_retain_300_and_status_projects_the_newest_30() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        for index in 1..=305_u64 {
            execute(
                &mut database,
                &request(
                    "integration.event.record",
                    11,
                    json!({
                        "user_id":11,"project_root":"/retention",
                        "task_id":"","kind":"probe",
                        "message":format!("event-{index}"),"detail":"",
                        "now":index as f64
                    }),
                    Some(&format!("integration-retention-{index}")),
                ),
                index,
            )
            .unwrap();
        }
        let status = execute(
            &mut database,
            &request(
                "integration.status",
                11,
                json!({"user_id":11,"project_root":"/retention"}),
                None,
            ),
            306,
        )
        .unwrap();
        let status_events = status["events"].as_array().unwrap();
        assert_eq!(status_events.len(), 30);
        assert_eq!(status_events.first().unwrap()["message"], "event-305");
        assert_eq!(status_events.last().unwrap()["message"], "event-276");

        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            crate::generated_tofudb_ir::INTEGRATION_EVENT_NAMESPACE,
            b"",
        )
        .unwrap();
        assert_eq!(
            database
                .entity_scan(&mut transaction, &start, &end, 301)
                .unwrap()
                .len(),
            300
        );
    }

    fn assert_integration_register_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 1_000).unwrap(),
            json!({"ok":true})
        );
        let status = execute(
            &mut recovered,
            &request(
                "integration.status",
                11,
                json!({"user_id":11,"project_root":"/shared-project"}),
                None,
            ),
            1_001,
        )
        .unwrap();
        assert_eq!(status["rows"].as_array().unwrap().len(), 1);
        assert_eq!(status["events"].as_array().unwrap().len(), 1);
        assert_eq!(status["rows"][0]["task_id"], "fault");
        assert_eq!(status["events"][0]["kind"], "registered");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
    }

    #[test]
    fn every_integration_register_fault_recovers_document_indexes_event_receipt_and_outbox() {
        let command = integration_register_request(11, "fault", 100.0);
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 100);
            drop(database);
            assert_integration_register_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 100);
            drop(database);
            assert_integration_register_fault_replay_converges(vfs, &command);
        }
    }

    fn seeded_swarm_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "swarm.session.save",
                11,
                json!({
                    "swarm_key":"swarm-fault","conv_id":"conversation",
                    "task_id":"task","status":"running","specs":[{"id":"agent-a"}],
                    "config":{"user_id":11},"now_ms":1_000
                }),
                Some("swarm-fault-session-save"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_swarm_checkpoint_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"saved":true})
        );
        let session = execute(
            &mut recovered,
            &request(
                "swarm.session.get",
                11,
                json!({"swarm_key":"swarm-fault"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(session["agents"].as_array().unwrap().len(), 1);
        assert_eq!(session["agents"][0]["agent_id"], "agent-a");
        assert_eq!(session["agents"][0]["result"]["final_answer"], "done");
        let resumable = execute(
            &mut recovered,
            &request("swarm.resumable.list", 11, json!({}), None),
            3_002,
        )
        .unwrap();
        assert_eq!(resumable.as_array().unwrap().len(), 1);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_swarm_checkpoint_fault_recovers_core_state_index_receipt_and_outbox_atomically() {
        let command = swarm_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_swarm_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_swarm_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_swarm_checkpoint_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_swarm_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_swarm_checkpoint_fault_replay_converges(vfs, &command);
        }
    }

    fn seeded_queue_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id":"queue-fault-conversation","user_id":11}),
                Some("queue-fault-conversation-create"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_queue_enqueue_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["queueId"], "queue-fault-item");
        assert_eq!(replayed["deduped"], false);
        let listed = execute(
            &mut recovered,
            &request(
                "queue.list",
                11,
                json!({"conv_id":"queue-fault-conversation","user_id":11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["payload"]["text"], "durable");
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "queue.list",
                    12,
                    json!({"conv_id":"queue-fault-conversation","user_id":12}),
                    None,
                ),
                3_001,
            )
            .unwrap(),
            json!([])
        );
        let global = execute(
            &mut recovered,
            &request("queue.conversations.list_all", 11, json!({}), None),
            3_002,
        )
        .unwrap();
        assert_eq!(
            global,
            json!([{"userId":11,"convId":"queue-fault-conversation"}])
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_queue_enqueue_fault_recovers_core_state_claim_order_summary_receipt_and_outbox() {
        let command = queue_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_queue_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_queue_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_queue_enqueue_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_queue_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_queue_enqueue_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn queue_idle_reap_is_not_receipted_and_same_command_can_later_release_work() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[61; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let reap = request(
            "queue.reap",
            11,
            json!({"now_ms":100}),
            Some("queue-reap-reusable"),
        );
        assert_eq!(
            execute(&mut database, &reap, 1_000).unwrap(),
            json!({"ok":false,"conversations":[]})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            0
        );
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id":"queue-reap-conversation","user_id":11}),
                Some("queue-reap-conversation-create"),
            ),
            1_001,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "queue.enqueue",
                11,
                json!({
                    "conv_id":"queue-reap-conversation","user_id":11,
                    "queue_id":"queue-reap-item","message":{"text":"pending"},
                    "config":{},"kind":"real","created_at_ms":1
                }),
                Some("queue-reap-enqueue"),
            ),
            1_002,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "queue.dequeue",
                11,
                json!({
                    "conv_id":"queue-reap-conversation","user_id":11,
                    "now_ms":1,"lease_ms":1
                }),
                Some("queue-reap-dequeue"),
            ),
            1_003,
        )
        .unwrap();
        assert_eq!(
            execute(&mut database, &reap, 1_004).unwrap(),
            json!({
                "ok":true,
                "conversations":[{"userId":11,"convId":"queue-reap-conversation"}]
            })
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            4
        );
    }

    fn seeded_expired_queue_lease(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = seeded_queue_database(vfs.clone());
        execute(&mut database, &queue_fault_request(), 2_000).unwrap();
        execute(
            &mut database,
            &request(
                "queue.dequeue",
                11,
                json!({
                    "conv_id":"queue-fault-conversation", "user_id":11,
                    "now_ms":2_001, "lease_ms":1
                }),
                Some("queue-fault-dequeue"),
            ),
            2_001,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_queue_reap_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({
                "ok":true,
                "conversations":[{"userId":11,"convId":"queue-fault-conversation"}]
            })
        );
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            4
        );
        let dequeued = execute(
            &mut recovered,
            &request(
                "queue.dequeue",
                11,
                json!({
                    "conv_id":"queue-fault-conversation", "user_id":11,
                    "now_ms":3_001, "lease_ms":1
                }),
                Some("queue-fault-dequeue-after-reap"),
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(dequeued["queueId"], "queue-fault-item");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            5
        );
    }

    #[test]
    fn every_queue_reap_fault_recovers_lease_index_state_receipt_and_outbox_atomically() {
        let command = request(
            "queue.reap",
            11,
            json!({"now_ms":2_003}),
            Some("queue-fault-reap"),
        );
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_expired_queue_lease(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_003).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_expired_queue_lease(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_003);
            drop(database);
            assert_queue_reap_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_expired_queue_lease(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_003);
            drop(database);
            assert_queue_reap_fault_replay_converges(vfs, &command);
        }
    }

    fn goal_policy_fixture() -> Value {
        json!({
            "solutionHorizon":"long_term","rootCauseRequired":true,
            "verificationEvidenceRequired":true,
            "temporaryPatchPolicy":"reject_when_robust_solution_is_in_scope",
            "objectiveFidelity":"never_shrink_original_scope",
            "iterationBudget":{"windowDefault":40,"windowHardCeiling":64,"policy":"checkpoint_extend","stallBreaker":"zero_state_changes_and_zero_verified_progress_in_window"},
            "workerContext":{"policy":"conversation","history":"model_messages_with_tool_results","compaction":"ordinary_chat","reviewer":"isolated_latest_work"},
            "directive":"Pursue the stated objective for durable long-term benefit. Diagnose and fix root causes, require concrete verification evidence, and do not substitute a temporary patch when a robust maintainable solution is within the delegated scope. Never shrink, narrow, or redefine the objective to fit what is easy to finish now: work is aligned only when it makes the full requested end state more true, and a smaller, safer, or easier-to-verify substitute that preserves a different end state is misaligned. Completion means the complete original scope is true and verified, not the largest subset that fits the moment."
        })
    }

    fn goal_start_fault_request() -> Request {
        request(
            "goal.run.start",
            11,
            json!({
                "run_id":"goal-fault-second","conversation_id":"goal-fault-conversation",
                "objective":"second objective","user_id":11,"tenant_id":"label",
                "definition":{"nodes":[{"id":"second"}]},
                "policy":goal_policy_fixture()
            }),
            Some("goal-fault-second-start"),
        )
    }

    fn seeded_goal_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "goal.run.start",
                11,
                json!({
                    "run_id":"goal-fault-first","conversation_id":"goal-fault-conversation",
                    "objective":"first objective","user_id":11,"tenant_id":"label",
                    "definition":{"nodes":[{"id":"first"}]},
                    "policy":goal_policy_fixture()
                }),
                Some("goal-fault-first-start"),
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_goal_start_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["created"], true);
        assert_eq!(replayed["run"]["runId"], "goal-fault-second");
        assert_eq!(replayed["run"]["status"], "active");
        let first = execute(
            &mut recovered,
            &request(
                "goal.run.get",
                11,
                json!({"run_id":"goal-fault-first","user_id":11,"tenant_id":"label"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(first["status"], "cancelled");
        assert_eq!(first["reason"], "superseded_by_new_goal");
        let latest = execute(
            &mut recovered,
            &request(
                "goal.run.latest",
                11,
                json!({"conversation_id":"goal-fault-conversation","user_id":11,"tenant_id":"label"}),
                None,
            ),
            3_002,
        )
        .unwrap();
        assert_eq!(latest["runId"], "goal-fault-second");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_goal_start_fault_recovers_supersession_events_claims_receipt_and_outbox_atomically() {
        let command = goal_start_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_goal_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_goal_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_goal_start_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_goal_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_goal_start_fault_replay_converges(vfs, &command);
        }
    }

    fn timer_create_request(
        owner: u64,
        timer_id: &str,
        conversation_id: &str,
        created_at: &str,
        command_id: &str,
    ) -> Request {
        request(
            "timer.create",
            owner,
            json!({
                "timer_id": timer_id,
                "user_id": owner,
                "conv_id": conversation_id,
                "check_instruction": "Check status",
                "continuation_message": "Continue when ready",
                "poll_interval": 60,
                "max_polls": 10,
                "created_at": created_at,
                "updated_at": created_at,
                "tools_config": {"web": false}
            }),
            Some(command_id),
        )
    }

    fn timer_poll_request(operation: &str, timer_id: &str, command_id: &str) -> Request {
        request(
            operation,
            11,
            json!({
                "timer_id": timer_id,
                "user_id": 11,
                "poll_id": "poll-fault",
                "poll_time": "2026-09-04T01:00:00",
                "decision": "run",
                "reason": "ready",
                "check_output": "ready",
                "tokens_used": 7,
                "model": "model-x",
                "raw_output": "raw",
                "tier": "llm",
                "predicate_matched": 1,
                "llm_agreed": 1
            }),
            Some(command_id),
        )
    }

    #[test]
    fn timer_code_only_admission_preserves_unlimited_and_rejects_empty_condition() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut create = timer_create_request(11, "timer-code", "conv-code", "2026-10-04", "timer-code-create");
        let mut payload: Value = serde_json::from_slice(&create.payload).unwrap();
        payload["check_instruction"] = json!("");
        payload["condition_command"] = json!("test -f ready");
        payload["condition_kind"] = json!("code");
        payload["max_polls"] = json!(0);
        create.payload = serde_json::to_vec(&payload).unwrap();
        let result = execute(&mut database, &create, 1_000).unwrap();
        assert_eq!(result["timer"]["condition_kind"], "code");
        assert_eq!(result["timer"]["max_polls"], 0);
        assert_eq!(execute(&mut database, &create, 1_000).unwrap(), result);
        payload["timer_id"] = json!("timer-invalid");
        payload["condition_command"] = json!("");
        create.command_id = Some("timer-invalid-create".to_owned());
        create.payload = serde_json::to_vec(&payload).unwrap();
        assert!(matches!(execute(&mut database, &create, 1_000), Err(SemanticExecutionError::InvalidPayload)));
    }

    #[test]
    fn timer_lifecycle_is_owner_scoped_globally_ordered_bounded_and_poll_idempotent() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[65; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        database.configure_timer_live_capacity(1).unwrap();

        execute(
            &mut database,
            &timer_create_request(11, "timer-a", "conv-a", "2026-09-01", "timer-create-a"),
            1_000,
        )
        .unwrap();
        execute(
            &mut database,
            &timer_create_request(12, "timer-b", "conv-b", "2026-09-02", "timer-create-b"),
            1_001,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &timer_create_request(11, "timer-a", "conv-c", "2026-09-03", "timer-create-c"),
                1_002,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "timer.get",
                    12,
                    json!({"timer_id":"timer-a","user_id":12}),
                    None,
                ),
                1_003,
            )
            .unwrap(),
            Value::Null
        );
        let global = execute(
            &mut database,
            &request("timer.active.list_all", 11, json!({"limit":20}), None),
            1_004,
        )
        .unwrap();
        assert_eq!(global.as_array().unwrap().len(), 2);
        assert_eq!(global[0]["id"], "timer-a");
        assert_eq!(global[1]["id"], "timer-b");

        let append = timer_poll_request("timer.poll.append", "timer-a", "timer-poll-append");
        let inserted = execute(&mut database, &append, 1_005).unwrap();
        assert_eq!(inserted["inserted"], true);
        let duplicate = timer_poll_request(
            "timer.poll.append",
            "timer-a",
            "timer-poll-append-duplicate",
        );
        let duplicate_result = execute(&mut database, &duplicate, 1_006).unwrap();
        assert_eq!(duplicate_result["inserted"], false);
        assert_eq!(duplicate_result["id"], inserted["id"]);

        let commit = request(
            "timer.poll.commit",
            11,
            json!({
                "timer_id":"timer-a", "user_id":11,
                "poll_id":"timer-commit", "poll_time":"2026-09-04T02:00:00",
                "decision":"run", "reason":"ready", "check_output":"ready",
                "tokens_used":8, "model":"model-x", "raw_output":"raw",
                "tier":"llm", "predicate_matched":1, "llm_agreed":1
            }),
            Some("timer-poll-commit"),
        );
        let committed = execute(&mut database, &commit, 1_007).unwrap();
        assert_eq!(committed["inserted"], true);
        assert_eq!(committed["advanced"], true);
        assert_eq!(execute(&mut database, &commit, 1_008).unwrap(), committed);
        let timer = execute(
            &mut database,
            &request(
                "timer.get",
                11,
                json!({"timer_id":"timer-a","user_id":11}),
                None,
            ),
            1_009,
        )
        .unwrap();
        assert_eq!(timer["poll_count"], 1);
        let polls = execute(
            &mut database,
            &request(
                "timer.poll.log",
                11,
                json!({"timer_id":"timer-a","user_id":11,"limit":20}),
                None,
            ),
            1_010,
        )
        .unwrap();
        assert_eq!(polls.as_array().unwrap().len(), 2);
        assert_eq!(polls[0]["poll_id"], "timer-commit");
    }

    #[test]
    fn conversation_delete_atomically_retires_queue_and_timer_executable_state() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[64; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id":"timer-conversation","user_id":11}),
                Some("timer-conversation-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut database,
            &timer_create_request(
                11,
                "timer-delete",
                "timer-conversation",
                "2026-09-01",
                "timer-delete-create",
            ),
            1_001,
        )
        .unwrap();
        let mut poll = timer_poll_request("timer.poll.append", "timer-delete", "timer-delete-poll");
        poll.payload = serde_json::to_vec(&json!({
            "timer_id":"timer-delete", "user_id":11, "poll_id":"delete-poll",
            "poll_time":"2026-09-02", "decision":"wait", "reason":"pending"
        }))
        .unwrap();
        execute(&mut database, &poll, 1_002).unwrap();
        execute(
            &mut database,
            &request(
                "queue.enqueue",
                11,
                json!({
                    "conv_id":"timer-conversation","user_id":11,
                    "queue_id":"queue-delete","message":{"text":"pending"},
                    "config":{},"kind":"real","created_at_ms":3
                }),
                Some("queue-delete-create"),
            ),
            1_002,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "queue.autopilot.arm",
                11,
                json!({
                    "conv_id":"timer-conversation","user_id":11,
                    "queue_id":"queue-delete-marker","config":{}
                }),
                Some("queue-delete-marker-create"),
            ),
            1_002,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.delete",
                11,
                json!({"conv_id":"timer-conversation","user_id":11}),
                Some("timer-conversation-delete"),
            ),
            1_003,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "timer.get",
                    11,
                    json!({"timer_id":"timer-delete","user_id":11}),
                    None,
                ),
                1_004,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut database,
                &request("timer.history", 11, json!({"user_id":11}), None),
                1_005,
            )
            .unwrap(),
            false
        );
        assert_eq!(
            execute(
                &mut database,
                &request("timer.active.count", 11, json!({"user_id":11}), None),
                1_006,
            )
            .unwrap(),
            0
        );
        execute(
            &mut database,
            &request(
                "conversation.restore",
                11,
                json!({"conv_id":"timer-conversation","user_id":11}),
                Some("timer-conversation-restore"),
            ),
            1_007,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request("timer.list", 11, json!({"user_id":11,"limit":20}), None),
                1_008,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "queue.list",
                    11,
                    json!({"conv_id":"timer-conversation","user_id":11}),
                    None,
                ),
                1_009,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "queue.autopilot.get",
                    11,
                    json!({"conv_id":"timer-conversation","user_id":11}),
                    None,
                ),
                1_010,
            )
            .unwrap(),
            Value::Null
        );
    }

    fn seeded_timer_poll_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &timer_create_request(
                11,
                "timer-fault",
                "fault-conversation",
                "2026-09-04T00:00:00",
                "timer-fault-create",
            ),
            1_000,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn assert_timer_poll_commit_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["inserted"], true);
        assert_eq!(replayed["advanced"], true);
        let timer = execute(
            &mut recovered,
            &request(
                "timer.get",
                11,
                json!({"timer_id":"timer-fault","user_id":11}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(timer["poll_count"], 1);
        assert_eq!(timer["last_poll_decision"], "run");
        let polls = execute(
            &mut recovered,
            &request(
                "timer.poll.log",
                11,
                json!({"timer_id":"timer-fault","user_id":11,"limit":20}),
                None,
            ),
            3_002,
        )
        .unwrap();
        assert_eq!(polls.as_array().unwrap().len(), 1);
        assert_eq!(polls[0]["poll_id"], "poll-fault");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            2
        );
    }

    #[test]
    fn every_timer_poll_commit_fault_recovers_ledger_progress_receipt_and_outbox_atomically() {
        let command = timer_poll_request("timer.poll.commit", "timer-fault", "timer-fault-commit");
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_timer_poll_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_timer_poll_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_timer_poll_commit_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_timer_poll_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_timer_poll_commit_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn scheduler_crud_is_owner_scoped_ordered_indexed_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[66; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let create = |task_id: &str, created_at: &str, command_id: &str| {
            request(
                "scheduler.task.create",
                11,
                json!({
                    "task_id": task_id,
                    "user_id": 11,
                    "name": task_id,
                    "schedule": "*/5 * * * *",
                    "task_type": "agent",
                    "command": "status",
                    "created_at": created_at,
                    "updated_at": created_at,
                    "target_conv_id": "conv-scheduler",
                    "tools_config": {"web": false}
                }),
                Some(command_id),
            )
        };
        let older = create("scheduler-a", "2026-09-01", "scheduler-create-a");
        let newer = create("scheduler-b", "2026-09-02", "scheduler-create-b");
        let created = execute(&mut database, &older, 1_000).unwrap();
        assert_eq!(created["applied"], true);
        assert_eq!(created["task"]["enabled"], 1);
        assert_eq!(created["task"]["notify_on_failure"], 1);
        assert_eq!(created["task"]["max_runtime"], 300);
        assert_eq!(execute(&mut database, &older, 9_000).unwrap(), created);
        execute(&mut database, &newer, 1_001).unwrap();

        let listed = execute(
            &mut database,
            &request(
                "scheduler.task.list",
                11,
                json!({"user_id":11,"limit":1,"enabled_only":false}),
                None,
            ),
            1_002,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["id"], "scheduler-b");

        assert_eq!(
            execute(
                &mut database,
                &request(
                    "scheduler.task.update",
                    11,
                    json!({
                        "task_id":"scheduler-b", "user_id":11,
                        "enabled":0, "description":"paused", "now":"later"
                    }),
                    Some("scheduler-update-b"),
                ),
                1_003,
            )
            .unwrap(),
            json!({"changed":true})
        );
        let enabled = execute(
            &mut database,
            &request(
                "scheduler.task.list",
                11,
                json!({"user_id":11,"limit":0,"enabled_only":true}),
                None,
            ),
            1_004,
        )
        .unwrap();
        assert_eq!(enabled.as_array().unwrap().len(), 1);
        assert_eq!(enabled[0]["id"], "scheduler-a");
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "scheduler.task.get",
                    12,
                    json!({"task_id":"scheduler-a","user_id":12}),
                    None,
                ),
                1_005,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "scheduler.task.delete",
                    11,
                    json!({"task_id":"scheduler-a","user_id":11}),
                    Some("scheduler-delete-a"),
                ),
                1_006,
            )
            .unwrap(),
            json!({"deleted":true})
        );
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            4
        );
    }

    #[test]
    fn scheduler_ensure_adopts_oldest_legacy_and_global_feed_spans_owners() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[67; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let create = |owner: u64, task_id: &str, created_at: &str, command_id: &str| {
            request(
                "scheduler.task.create",
                owner,
                json!({
                    "task_id":task_id, "user_id":owner, "name":"Builtin",
                    "schedule":"0 * * * *", "task_type":"agent", "command":"maintain",
                    "created_at":created_at, "updated_at":created_at, "tools_config":{}
                }),
                Some(command_id),
            )
        };
        execute(
            &mut database,
            &create(11, "legacy-old", "2026-01-01", "legacy-old"),
            1,
        )
        .unwrap();
        execute(
            &mut database,
            &create(11, "legacy-new", "2026-01-02", "legacy-new"),
            2,
        )
        .unwrap();
        execute(
            &mut database,
            &create(12, "other-owner", "2026-01-04", "other-owner"),
            3,
        )
        .unwrap();
        execute(
            &mut database,
            &request(
                "scheduler.task.ensure",
                11,
                json!({
                    "task_id":"keyed-short-lived", "user_id":11, "system_key":"builtin",
                    "name":"Other name", "schedule":"0 * * * *", "task_type":"agent",
                    "command":"maintain", "created_at":"2026-01-03", "updated_at":"2026-01-03",
                    "tools_config":{}
                }),
                Some("keyed-create"),
            ),
            4,
        )
        .unwrap();
        let adopted = execute(
            &mut database,
            &request(
                "scheduler.task.ensure",
                11,
                json!({
                    "task_id":"unused", "user_id":11, "system_key":"builtin",
                    "name":"Builtin", "schedule":"30 * * * *", "task_type":"agent",
                    "command":"maintain", "description":"reconciled",
                    "created_at":"2026-01-05", "updated_at":"2026-01-05", "tools_config":{}
                }),
                Some("keyed-adopt"),
            ),
            5,
        )
        .unwrap();
        assert_eq!(adopted["created"], false);
        assert_eq!(adopted["updated"], true);
        assert_eq!(adopted["task"]["id"], "legacy-old");
        assert_eq!(adopted["task"]["system_key"], "builtin");
        for retired in ["legacy-new", "keyed-short-lived"] {
            assert_eq!(
                execute(
                    &mut database,
                    &request(
                        "scheduler.task.get",
                        11,
                        json!({"task_id":retired,"user_id":11}),
                        None,
                    ),
                    6,
                )
                .unwrap(),
                Value::Null
            );
        }
        let global = execute(
            &mut database,
            &request(
                "scheduler.task.list_all",
                11,
                json!({"limit":20,"enabled_only":true}),
                None,
            ),
            7,
        )
        .unwrap();
        assert_eq!(global.as_array().unwrap().len(), 2);
        assert_eq!(global[0]["id"], "other-owner");
        assert_eq!(global[1]["id"], "legacy-old");
    }

    fn scheduler_fault_create() -> Request {
        request(
            "scheduler.task.create",
            11,
            json!({
                "task_id":"scheduler-fault", "user_id":11,
                "name":"Fault task", "schedule":"*/5 * * * *",
                "task_type":"agent", "command":"status",
                "created_at":"2026-09-04T00:00:00",
                "updated_at":"2026-09-04T00:00:00", "tools_config":{}
            }),
            Some("scheduler-fault-create"),
        )
    }

    fn assert_scheduler_create_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let replayed = execute(&mut recovered, command, 3_000).unwrap();
        assert_eq!(replayed["task"]["id"], "scheduler-fault");
        let listed = execute(
            &mut recovered,
            &request(
                "scheduler.task.list",
                11,
                json!({"user_id":11,"limit":20,"enabled_only":false}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["id"], "scheduler-fault");
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1
        );
        assert_eq!(
            execute(
                &mut recovered,
                &request(
                    "scheduler.task.create",
                    12,
                    json!({
                        "task_id":"scheduler-fault", "user_id":12,
                        "name":"Collision", "schedule":"0 * * * *",
                        "command":"status", "created_at":"later",
                        "updated_at":"later", "tools_config":{}
                    }),
                    Some("scheduler-cross-owner-collision"),
                ),
                3_002,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
    }

    #[test]
    fn every_scheduler_create_fault_recovers_document_claim_count_indexes_and_outbox_atomically() {
        let command = scheduler_fault_create();
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_scheduler_create_fault_replay_converges(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_scheduler_create_fault_replay_converges(vfs, &command);
        }
    }

    #[test]
    fn turn_event_record_atomically_advances_projection_attempt_sync_and_carried_event() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "event-record-conversation",
                    "user_id": 11,
                    "title": "Event record",
                    "settings": {}
                }),
                Some("event-record-conversation-create"),
            ),
            1_000,
        )
        .unwrap();
        let seeded = execute(
            &mut database,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "event-record-conversation",
                    "user_id": 11,
                    "command_id": "event-record-seed",
                    "actor": "assistant",
                    "kind": "reply",
                    "status": "completed",
                    "projection": {"content": "", "thinking": "", "segments": []},
                    "settlement": {"outcome": "completed", "cause": "seed", "resumeOptions": []},
                    "turn_id": "event-record-turn",
                    "created_at": 1_100
                }),
                Some("event-record-seed-command"),
            ),
            1_100,
        )
        .unwrap();
        assert_eq!(seeded["turn"]["projectionRevision"], 1);
        let created_attempt = execute(
            &mut database,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "event-record-conversation",
                    "turn_id": "event-record-turn",
                    "user_id": 11,
                    "command_id": "event-record-attempt",
                    "operation": "regenerate",
                    "target_actor": "assistant",
                    "target_kind": "reply",
                    "expected_projection_revision": 1,
                    "config": {}
                }),
                Some("event-record-attempt-command"),
            ),
            1_150,
        )
        .unwrap();
        let attempt_id = created_attempt["attempt"]["attemptId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id": attempt_id, "task_id": "event-task", "user_id": 11}),
                Some("event-record-bind"),
            ),
            1_200,
        )
        .unwrap();
        let live = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "task_id": "event-task",
                    "slim": true,
                    "content": "hello",
                    "thinking": "working",
                    "event_payload": {"phase": "generating", "projection": {"duplicate": true}},
                    "task_event": {
                        "task_id": "event-task",
                        "sequence": 1,
                        "event": {"type": "delta", "content": "hello"}
                    },
                    "now": 1_300
                }),
                Some("event-record-live"),
            ),
            1_300,
        )
        .unwrap();
        assert_eq!(live["applied"], true);
        assert_eq!(live["status"], "running");
        assert!(live["task_event"].is_object());
        let live_revision = live["projection_revision"].as_u64().unwrap();
        let replay = execute(
            &mut database,
            &request(
                "turn.events.list",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "projection_mode": "patch"
                }),
                None,
            ),
            1_301,
        )
        .unwrap();
        let live_event = replay.as_array().unwrap().last().unwrap();
        assert_eq!(
            live_event["payload"]["projectionPatch"]["baseRevision"],
            live_revision - 1
        );
        assert!(live_event["payload"].get("projection").is_none());

        execute(
            &mut database,
            &request(
                "turn.perception.record",
                11,
                json!({
                    "conversation_id": "event-record-conversation",
                    "turn_id": "event-record-turn",
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "observation": {
                        "observationId": "event:paint:1",
                        "attemptId": attempt_id,
                        "kind": "phase_painted",
                        "clientId": "page",
                        "receivedAt": 1_310,
                        "paintedAt": 1_320
                    }
                }),
                Some("event-record-perception"),
            ),
            1_320,
        )
        .unwrap();
        let terminal = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "task_id": "event-task",
                    "terminal": true,
                    "status": "completed",
                    "projection": {
                        "content": "hello world",
                        "thinking": "done",
                        "segments": [],
                        "timingTrace": {"status": "completed", "spans": []}
                    },
                    "settlement": {"outcome": "completed", "cause": "finished", "resumeOptions": []},
                    "event_payload": {"phase": null},
                    "now": 1_400
                }),
                Some("event-record-terminal"),
            ),
            1_400,
        )
        .unwrap();
        assert_eq!(terminal["applied"], true);
        assert_eq!(terminal["projection_revision"], live_revision + 1);
        let turn = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "event-record-conversation",
                    "turn_id": "event-record-turn",
                    "user_id": 11
                }),
                None,
            ),
            1_500,
        )
        .unwrap();
        assert_eq!(turn["status"], "completed");
        assert_eq!(turn["projection"]["content"], "hello world");
        assert_eq!(
            turn["projection"]["timingTrace"]["clientObservations"][0]["observationId"],
            "event:paint:1"
        );
        let attempt = execute(
            &mut database,
            &request(
                "turn.attempt.get",
                11,
                json!({"attempt_id": attempt_id, "user_id": 11}),
                None,
            ),
            1_501,
        )
        .unwrap();
        assert_eq!(attempt["status"], "completed");
        let events = execute(
            &mut database,
            &request(
                "event.list",
                11,
                json!({"task_id": "event-task", "user_id": 11}),
                None,
            ),
            1_502,
        )
        .unwrap();
        assert_eq!(events.as_array().unwrap().len(), 1);
    }

    /// Regression for the 2026-09-16 cutover wedge: producers diff against
    /// the stable-segment read, so a positional `segments[0]` patch must
    /// apply even when the stored pair projection still has `segments: []`,
    /// and the committed replay patch must remain applicable to the exact
    /// previous wire document (normalization delta included).
    #[test]
    fn turn_event_record_applies_structural_patch_over_unseeded_storage() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "segments-conversation",
                    "user_id": 11,
                    "title": "Segments",
                    "settings": {}
                }),
                Some("segments-conversation-create"),
            ),
            1_000,
        )
        .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "segments-conversation",
                    "user_id": 11,
                    "command_id": "segments-pair",
                    "input_projection": {"content": "question"},
                    "config": {},
                    "now": 1_100
                }),
                Some("segments-pair-command"),
            ),
            1_100,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let turn_id = pair["turn"]["turnId"].as_str().unwrap();
        assert_eq!(pair["turn"]["projectionRevision"], 1);
        assert_eq!(
            pair["turn"]["projection"]["segments"],
            json!([]),
            "the wire base starts without the producer seed"
        );
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id": attempt_id, "task_id": "segments-task", "user_id": 11}),
                Some("segments-bind"),
            ),
            1_200,
        )
        .unwrap();
        // The exact producer patch family from the wedged production turn:
        // positional operations against the stable-segment baseline.
        let structural = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "task_id": "segments-task",
                    "event_type": "projection_updated",
                    "event_payload": {"phase": null, "updateKind": "delta"},
                    "projection_patch": {
                        "version": 1,
                        "baseRevision": 2,
                        "targetRevision": 3,
                        "operations": [
                            {"op": "set", "path": ["segments", 0, "blockId"], "value": "thinking:terminal"},
                            {"op": "set", "path": ["segments", 0, "deliverable"], "value": false},
                            {"op": "append_text", "path": ["segments", 0, "text"], "value": "The"},
                            {"op": "set", "path": ["segments", 0, "type"], "value": "thinking"},
                            {"op": "append", "path": ["segments"], "value": [{
                                "type": "text",
                                "blockId": "text:terminal",
                                "text": "",
                                "deliverable": true,
                                "terminal": true
                            }]},
                            {"op": "append_text", "path": ["thinking"], "value": "The"}
                        ]
                    },
                    "projection_segments_stable": true,
                    "task_event": {
                        "task_id": "segments-task",
                        "sequence": 1,
                        "event": {"type": "delta", "thinking": "The"}
                    },
                    "now": 1_300
                }),
                Some("segments-structural"),
            ),
            1_300,
        )
        .unwrap();
        assert_eq!(structural["applied"], true);
        // turn.attempt.bind already advanced the pair revision 1 -> 2.
        assert_eq!(structural["projection_revision"], 3);
        let turn = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "segments-conversation",
                    "turn_id": turn_id,
                    "user_id": 11
                }),
                None,
            ),
            1_400,
        )
        .unwrap();
        assert_eq!(turn["projection"]["thinking"], "The");
        assert_eq!(
            turn["projection"]["segments"],
            json!([
                {
                    "type": "thinking",
                    "blockId": "thinking:terminal",
                    "text": "The",
                    "deliverable": false,
                    "terminal": true
                },
                {
                    "type": "text",
                    "blockId": "text:terminal",
                    "text": "",
                    "deliverable": true,
                    "terminal": true
                }
            ]),
            "the stored next projection is the seeded producer baseline"
        );
        // The committed replay patch applies to the exact previous wire
        // document (segments: []), carrying the normalization delta.
        let replay = execute(
            &mut database,
            &request(
                "turn.events.list",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "projection_mode": "patch"
                }),
                None,
            ),
            1_401,
        )
        .unwrap();
        let structural_event = replay.as_array().unwrap().last().unwrap().clone();
        let replay_patch = structural_event["payload"]["projectionPatch"]
            .as_object()
            .unwrap()
            .clone();
        let wire_base = json!({"content": "", "thinking": "", "segments": [], "toolRounds": []})
            .as_object()
            .unwrap()
            .clone();
        let rebuilt =
            crate::turn_projection_patch::apply_projection_patch(Some(&wire_base), &replay_patch)
                .unwrap();
        assert_eq!(
            Value::Object(rebuilt),
            turn["projection"].clone(),
            "raw replay bases converge on the seeded next projection"
        );
    }

    /// A slim delta leaves the stored segment mirror intentionally stale; the
    /// next structural patch normalizes that baseline, so the replay patch
    /// must be rebuilt from the raw wire document rather than reusing the
    /// client patch verbatim.
    #[test]
    fn turn_event_record_rebuilds_replay_patch_after_slim_baseline_drift() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "slim-drift-conversation",
                    "user_id": 11,
                    "title": "Slim drift",
                    "settings": {}
                }),
                Some("slim-drift-conversation-create"),
            ),
            1_000,
        )
        .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id": "slim-drift-conversation",
                    "user_id": 11,
                    "command_id": "slim-drift-pair",
                    "input_projection": {"content": "question"},
                    "config": {},
                    "now": 1_100
                }),
                Some("slim-drift-pair-command"),
            ),
            1_100,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let turn_id = pair["turn"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id": attempt_id, "task_id": "slim-drift-task", "user_id": 11}),
                Some("slim-drift-bind"),
            ),
            1_200,
        )
        .unwrap();
        let slim = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "task_id": "slim-drift-task",
                    "slim": true,
                    "content": "Hello",
                    "thinking": "",
                    "event_payload": {"phase": null, "updateKind": "delta"},
                    "task_event": {
                        "task_id": "slim-drift-task",
                        "sequence": 1,
                        "event": {"type": "delta", "content": "Hello"}
                    },
                    "now": 1_300
                }),
                Some("slim-drift-live"),
            ),
            1_300,
        )
        .unwrap();
        assert_eq!(slim["applied"], true);
        // turn.attempt.bind already advanced the pair revision 1 -> 2.
        assert_eq!(slim["projection_revision"], 3);
        // Producer baseline after the slim write: terminal text mirror synced
        // to the accumulated content. Its structural patch replaces the seed.
        let structural = execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "task_id": "slim-drift-task",
                    "event_type": "projection_updated",
                    "event_payload": {"phase": null, "updateKind": "round_end"},
                    "projection_patch": {
                        "version": 1,
                        "baseRevision": 3,
                        "targetRevision": 4,
                        "operations": [
                            {"op": "set", "path": ["segments", 0, "blockId"], "value": "thinking:terminal"},
                            {"op": "set", "path": ["segments", 0, "deliverable"], "value": false},
                            {"op": "set", "path": ["segments", 0, "text"], "value": "reason"},
                            {"op": "set", "path": ["segments", 0, "type"], "value": "thinking"},
                            {"op": "append", "path": ["segments"], "value": [{
                                "type": "text",
                                "blockId": "text:terminal",
                                "text": "Hello",
                                "deliverable": true,
                                "terminal": true
                            }]},
                            {"op": "set", "path": ["thinking"], "value": "reason"}
                        ]
                    },
                    "projection_segments_stable": true,
                    "task_event": {
                        "task_id": "slim-drift-task",
                        "sequence": 2,
                        "event": {"type": "round_end", "reason": "final"}
                    },
                    "now": 1_400
                }),
                Some("slim-drift-structural"),
            ),
            1_400,
        )
        .unwrap();
        assert_eq!(structural["applied"], true);
        assert_eq!(structural["projection_revision"], 4);
        let turn = execute(
            &mut database,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "slim-drift-conversation",
                    "turn_id": turn_id,
                    "user_id": 11
                }),
                None,
            ),
            1_500,
        )
        .unwrap();
        let replay = execute(
            &mut database,
            &request(
                "turn.events.list",
                11,
                json!({
                    "attempt_id": attempt_id,
                    "user_id": 11,
                    "projection_mode": "patch"
                }),
                None,
            ),
            1_501,
        )
        .unwrap();
        let structural_event = replay.as_array().unwrap().last().unwrap().clone();
        let replay_patch = structural_event["payload"]["projectionPatch"]
            .as_object()
            .unwrap()
            .clone();
        // The wire base after the slim write still has no segment mirror at
        // all: slim writes advance only the two text fields.
        let wire_base = json!({
            "content": "Hello",
            "thinking": "",
            "segments": [],
            "toolRounds": []
        })
        .as_object()
        .unwrap()
        .clone();
        let rebuilt =
            crate::turn_projection_patch::apply_projection_patch(Some(&wire_base), &replay_patch)
                .unwrap();
        assert_eq!(
            Value::Object(rebuilt),
            turn["projection"].clone(),
            "replay from the slim-stale wire base converges on the stored next"
        );
    }

    fn seeded_event_record_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String) {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        let attempt_id = "fault-event-attempt".to_owned();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: "fault-event-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: serde_json::to_vec(&json!({
                    "content": "before",
                    "thinking": "",
                    "largeStableEvidence": "x".repeat(128 * 1024),
                }))
                .unwrap(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "fault-event-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "fault-event-turn".to_owned(),
                attempt_id: Some(attempt_id.clone()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Fault event".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::attempt_bind(
            &database,
            &mut bind,
            &attempt_id,
            "fault-event-task",
            "",
            1_200,
        )
        .unwrap();
        database.commit(bind).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, attempt_id)
    }

    fn event_record_fault_request(attempt_id: &str) -> Request {
        request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id,
                "task_id": "fault-event-task",
                "user_id": 11,
                "slim": true,
                "content": "after",
                "thinking": "running",
                "task_event": {
                    "task_id": "fault-event-task", "sequence": 2,
                    "event": {"type": "delta", "content": "after"},
                    "preceding_events": [
                        {"task_id": "fault-event-task", "sequence": 0, "event": {"type": "delta", "content": "a"}},
                        {"task_id": "fault-event-task", "sequence": 1, "event": {"type": "delta", "content": "ft"}}
                    ]
                },
                "now": 1_300
            }),
            Some("fault-event-record"),
        )
    }

    fn terminal_event_record_fault_request(attempt_id: &str) -> Request {
        request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id,
                "task_id": "fault-event-task",
                "user_id": 11,
                "terminal": true,
                "status": "completed",
                "slim": true,
                "content": "final",
                "thinking": "done",
                "settlement": {
                    "outcome": "completed",
                    "cause": "finished",
                    "resumeOptions": []
                },
                "now": 1_400
            }),
            Some("fault-event-terminal"),
        )
    }

    fn recovered_event_record_state(
        vfs: std::sync::Arc<DeterministicVfs>,
        attempt_id: &str,
    ) -> (Value, Value, Value) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turn = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-event-conversation",
                    "turn_id": "fault-event-turn",
                    "user_id": 11
                }),
                None,
            ),
            2_000,
        )
        .unwrap();
        let attempt = execute(
            &mut recovered,
            &request(
                "turn.attempt.get",
                11,
                json!({"attempt_id": attempt_id, "user_id": 11}),
                None,
            ),
            2_001,
        )
        .unwrap();
        let replay = execute(
            &mut recovered,
            &request(
                "turn.events.list",
                11,
                json!({"attempt_id": attempt_id, "user_id": 11, "projection_mode": "patch"}),
                None,
            ),
            2_002,
        )
        .unwrap();
        (turn, attempt, replay)
    }

    fn assert_event_record_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        attempt_id: &str,
    ) {
        let (turn, attempt, replay) = recovered_event_record_state(vfs.clone(), attempt_id);
        let mut database = AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let rows = execute(&mut database, &request("event.list", 11,
            json!({"task_id": "fault-event-task", "after_sequence": -1, "limit": 10}), None), 1_500).unwrap();
        match turn["projectionRevision"].as_u64().unwrap() {
            2 => {
                assert_eq!(turn["projection"]["content"], "before");
                assert_eq!(attempt["status"], "pending");
                assert_eq!(replay.as_array().unwrap().len(), 1);
                assert!(rows.as_array().unwrap().is_empty());
            }
            3 => {
                assert_eq!(turn["projection"]["content"], "after");
                assert_eq!(attempt["status"], "running");
                assert_eq!(replay.as_array().unwrap().len(), 2);
                assert_eq!(rows.as_array().unwrap().len(), 3);
                for (sequence, row) in rows.as_array().unwrap().iter().enumerate() {
                    assert_eq!(row["sequence"], sequence as u64);
                }
            }
            revision => panic!("partial Turn event commit recovered at revision {revision}"),
        }
    }

    #[test]
    fn every_turn_event_commit_fault_recovers_only_an_atomic_prefix() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, attempt_id) = seeded_event_record_fault_database(baseline_vfs.clone());
        let baseline_request = event_record_fault_request(&attempt_id);
        let admitted = admit_request(
            &baseline_request,
            NegotiatedProtocol {
                protocol_version: PROTOCOL_VERSION,
                schema_id: STORAGE_SCHEMA_VERSION,
            },
            AuthenticatedScope {
                owner_id: 11,
                tenant_id: Some(7),
            },
            1_300,
        )
        .unwrap();
        let ir = compile_turn(admitted, 1_300).unwrap();
        let mut committed_notices = Vec::new();
        crate::transaction_ir::execute_transaction_ir_with_notices(
            &mut baseline,
            &ir,
            &mut committed_notices,
        )
        .unwrap();
        assert_eq!(committed_notices.len(), 1);
        assert_eq!(committed_notices[0].user_id, 11);
        assert_eq!(committed_notices[0].key, "fault-event-conversation");
        assert!(committed_notices[0].sequence > 0);
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, attempt_id) = seeded_event_record_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let mut notices = Vec::new();
            let result = crate::transaction_ir::execute_transaction_ir_with_notices(
                &mut database,
                &ir,
                &mut notices,
            );
            if result.is_err() {
                assert!(
                    notices.is_empty(),
                    "failed commit published a hint at {operation_number}"
                );
            } else {
                assert_eq!(notices, committed_notices);
            }
            drop(database);
            assert_event_record_recovers_atomic_prefix(vfs, &attempt_id);
        }
    }

    #[test]
    fn oversized_initial_projection_patch_fold_survives_every_commit_fault() {
        let content = "large-checkpoint".repeat(90_000);
        let prepare = |vfs: std::sync::Arc<DeterministicVfs>| {
            let (mut database, attempt) = seeded_event_record_fault_database(vfs.clone());
            database
                .configure_logical_outbox(&[72; 32], 16 * 1024 * 1024, 4 * 1024 * 1024)
                .unwrap();
            let mut request = event_record_fault_request(&attempt);
            let mut payload: Value = serde_json::from_slice(&request.payload).unwrap();
            payload["content"] = json!(content);
            request.payload = serde_json::to_vec(&payload).unwrap();
            vfs.arm_fault(None).unwrap();
            (database, attempt, request)
        };
        let baseline_vfs = prepared_vfs();
        let (mut database, attempt, request) = prepare(baseline_vfs.clone());
        execute(&mut database, &request, 1_300).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(database);
        let expected = recovered_event_record_state(baseline_vfs, &attempt);
        assert_eq!(expected.0["projection"]["content"], content);
        for (index, operation) in trace.iter().enumerate() {
            let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Interrupted)];
            if operation == &Operation::Write {
                actions.push(FaultAction::ShortWrite(7));
            }
            for action in actions {
                let vfs = prepared_vfs();
                let (mut database, attempt, request) = prepare(vfs.clone());
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action,
                }))
                .unwrap();
                let acknowledged = execute(&mut database, &request, 1_300).is_ok();
                drop(database);
                let recovered = recovered_event_record_state(vfs, &attempt);
                if recovered.0["projectionRevision"] == 2 {
                    assert!(!acknowledged, "lost acknowledged checkpoint at {index}");
                    assert_eq!(recovered.0["projection"]["content"], "before");
                    assert_eq!(recovered.1["status"], "pending");
                    assert_eq!(recovered.2.as_array().unwrap().len(), 1);
                } else {
                    assert_eq!(recovered, expected, "partial checkpoint at {index}");
                }
            }
        }
    }

    #[test]
    fn every_terminal_turn_event_fault_retires_projection_head_atomically() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, attempt_id) = seeded_event_record_fault_database(baseline_vfs.clone());
        execute(
            &mut baseline,
            &event_record_fault_request(&attempt_id),
            1_300,
        )
        .unwrap();
        let baseline_request = terminal_event_record_fault_request(&attempt_id);
        let admitted = admit_request(
            &baseline_request,
            NegotiatedProtocol {
                protocol_version: PROTOCOL_VERSION,
                schema_id: STORAGE_SCHEMA_VERSION,
            },
            AuthenticatedScope {
                owner_id: 11,
                tenant_id: Some(7),
            },
            1_400,
        )
        .unwrap();
        let ir = compile_turn(admitted, 1_400).unwrap();
        crate::transaction_ir::execute_transaction_ir(&mut baseline, &ir).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, attempt_id) = seeded_event_record_fault_database(vfs.clone());
            execute(
                &mut database,
                &event_record_fault_request(&attempt_id),
                1_300,
            )
            .unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &terminal_event_record_fault_request(&attempt_id),
                1_400,
            );
            drop(database);
            let (turn, attempt, replay) = recovered_event_record_state(vfs, &attempt_id);
            match turn["projectionRevision"].as_u64().unwrap() {
                3 => {
                    assert_eq!(turn["projection"]["content"], "after");
                    assert_eq!(turn["status"], "running");
                    assert_eq!(attempt["status"], "running");
                    assert_eq!(replay.as_array().unwrap().len(), 2);
                }
                4 => {
                    assert_eq!(turn["projection"]["content"], "final");
                    assert_eq!(turn["status"], "completed");
                    assert_eq!(attempt["status"], "completed");
                    assert_eq!(replay.as_array().unwrap().len(), 3);
                }
                revision => panic!("partial terminal Turn event recovered at revision {revision}"),
            }
        }
    }

    fn seeded_turn_retention_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String) {
        let (mut database, attempt_id) = seeded_event_record_fault_database(vfs.clone());
        execute(
            &mut database,
            &event_record_fault_request(&attempt_id),
            1_300,
        )
        .unwrap();
        execute(
            &mut database,
            &terminal_event_record_fault_request(&attempt_id),
            1_400,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, attempt_id)
    }

    fn sync_prune_fault_request() -> Request {
        request(
            "turn.sync.prune",
            11,
            json!({"user_id": 11, "created_before_ms": 2_000, "max_rows": 20_000}),
            Some("fault-sync-prune"),
        )
    }

    fn event_prune_fault_request() -> Request {
        request(
            "turn.events.prune",
            11,
            json!({
                "user_id": 11,
                "settled_before_ms": 2_000,
                "max_attempts": 16,
                "max_rows": 200_000
            }),
            Some("fault-attempt-event-prune"),
        )
    }

    fn reopen_retention_fault_database(vfs: std::sync::Arc<DeterministicVfs>) -> AuthorityDatabase {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        recovered
    }

    fn assert_retention_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        attempt_id: &str,
    ) {
        let mut recovered = reopen_retention_fault_database(vfs);
        let sync_before_replay = execute(
            &mut recovered,
            &request(
                "turn.sync.changes",
                11,
                json!({
                    "conversation_id": "fault-event-conversation",
                    "user_id": 11,
                    "after": 0,
                    "limit": 100
                }),
                None,
            ),
            1_498,
        )
        .unwrap();
        let sync_count = sync_before_replay["events"]
            .as_array()
            .map(Vec::len)
            .unwrap_or(0);
        assert!(matches!(sync_count, 0 | 4));
        let events_before_replay = execute(
            &mut recovered,
            &request(
                "turn.events.list",
                11,
                json!({"attempt_id": attempt_id, "user_id": 11, "projection_mode": "patch"}),
                None,
            ),
            1_499,
        )
        .unwrap();
        assert!(matches!(
            events_before_replay.as_array().unwrap().len(),
            0 | 3
        ));
        execute(&mut recovered, &sync_prune_fault_request(), 1_500).unwrap();
        execute(&mut recovered, &event_prune_fault_request(), 1_501).unwrap();
        let replay = execute(
            &mut recovered,
            &request(
                "turn.events.list",
                11,
                json!({"attempt_id": attempt_id, "user_id": 11, "projection_mode": "patch"}),
                None,
            ),
            1_502,
        )
        .unwrap();
        assert_eq!(replay, json!([]));
        let turn = execute(
            &mut recovered,
            &request(
                "turn.get",
                11,
                json!({
                    "conversation_id": "fault-event-conversation",
                    "turn_id": "fault-event-turn",
                    "user_id": 11
                }),
                None,
            ),
            1_503,
        )
        .unwrap();
        assert_eq!(turn["projection"]["content"], "final");
    }

    #[test]
    fn every_turn_retention_commit_fault_recovers_and_replay_converges() {
        for prune_request in [sync_prune_fault_request(), event_prune_fault_request()] {
            let baseline_vfs = prepared_vfs();
            let (mut baseline, _) = seeded_turn_retention_fault_database(baseline_vfs.clone());
            if prune_request.operation == "turn.events.prune" {
                execute(&mut baseline, &sync_prune_fault_request(), 1_499).unwrap();
                baseline_vfs.arm_fault(None).unwrap();
            }
            execute(&mut baseline, &prune_request, 1_500).unwrap();
            let trace = baseline_vfs.trace().unwrap();
            drop(baseline);

            for operation_number in 1..=trace.len() as u64 {
                let vfs = prepared_vfs();
                let (mut database, attempt_id) = seeded_turn_retention_fault_database(vfs.clone());
                if prune_request.operation == "turn.events.prune" {
                    execute(&mut database, &sync_prune_fault_request(), 1_499).unwrap();
                    vfs.arm_fault(None).unwrap();
                }
                vfs.arm_fault(Some(FaultRule {
                    operation_number,
                    action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
                }))
                .unwrap();
                let _ = execute(&mut database, &prune_request, 1_500);
                drop(database);
                assert_retention_fault_replay_converges(vfs, &attempt_id);
            }
            for (index, operation) in trace.iter().enumerate() {
                if operation != &Operation::Write {
                    continue;
                }
                let vfs = prepared_vfs();
                let (mut database, attempt_id) = seeded_turn_retention_fault_database(vfs.clone());
                if prune_request.operation == "turn.events.prune" {
                    execute(&mut database, &sync_prune_fault_request(), 1_499).unwrap();
                    vfs.arm_fault(None).unwrap();
                }
                vfs.arm_fault(Some(FaultRule {
                    operation_number: index as u64 + 1,
                    action: FaultAction::ShortWrite(7),
                }))
                .unwrap();
                let _ = execute(&mut database, &prune_request, 1_500);
                drop(database);
                assert_retention_fault_replay_converges(vfs, &attempt_id);
            }
        }
    }

    fn seeded_turn_recovery_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String) {
        let (mut database, attempt_id) = seeded_event_record_fault_database(vfs.clone());
        execute(
            &mut database,
            &event_record_fault_request(&attempt_id),
            1_300,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, attempt_id)
    }

    fn turn_recovery_fault_request() -> Request {
        request(
            "turn.recover",
            11,
            json!({"user_id": 11, "max_rows": 8, "max_bytes": 2_000_000}),
            Some("fault-turn-recover"),
        )
    }

    fn assert_turn_recovery_recovers_atomic_prefix(
        vfs: std::sync::Arc<DeterministicVfs>,
        attempt_id: &str,
    ) {
        let (turn, attempt, replay) = recovered_event_record_state(vfs, attempt_id);
        match turn["projectionRevision"].as_u64().unwrap() {
            3 => {
                assert_eq!(turn["status"], "running");
                assert_eq!(attempt["status"], "running");
                assert_eq!(replay.as_array().unwrap().len(), 2);
            }
            4 => {
                assert_eq!(turn["status"], "interrupted");
                assert_eq!(attempt["status"], "interrupted");
                assert_eq!(turn["settlement"]["cause"], "server_restart");
                assert_eq!(replay.as_array().unwrap().len(), 3);
                assert_eq!(replay[2]["payload"]["settlement"], turn["settlement"]);
            }
            revision => panic!("partial Turn recovery commit recovered at revision {revision}"),
        }
    }

    #[test]
    fn turn_recovery_is_bounded_and_every_commit_fault_is_atomic() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, attempt_id) = seeded_turn_recovery_fault_database(baseline_vfs.clone());
        let command = turn_recovery_fault_request();
        assert_eq!(
            execute(&mut baseline, &command, 1_400).unwrap(),
            json!({"recovered": 1, "remaining": 0})
        );
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_turn_recovery_recovers_atomic_prefix(baseline_vfs, &attempt_id);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, attempt_id) = seeded_turn_recovery_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_400);
            drop(database);
            assert_turn_recovery_recovers_atomic_prefix(vfs, &attempt_id);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, attempt_id) = seeded_turn_recovery_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_400);
            drop(database);
            assert_turn_recovery_recovers_atomic_prefix(vfs, &attempt_id);
        }
    }

    fn turn_pair_fault_request() -> Request {
        request(
            "turn.create_pair",
            11,
            json!({
                "conversation_id": "fault-turn-pair",
                "user_id": 11,
                "command_id": "fault-turn-pair-command",
                "input_projection": {"content": "durable pair"},
                "config": {"model": "fault-model"},
                "queue_binding": {"queueId": "fault-turn-pair-queue", "kind": "real"},
                "conversation_defaults": {"allowCreate": true},
                "now": 1_000
            }),
            Some("fault-turn-pair-receipt"),
        )
    }

    #[test]
    fn replayed_turn_pair_does_not_publish_a_new_commit_notice() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let request = turn_pair_fault_request();
        let admitted = admit_request(
            &request,
            NegotiatedProtocol {
                protocol_version: PROTOCOL_VERSION,
                schema_id: STORAGE_SCHEMA_VERSION,
            },
            AuthenticatedScope {
                owner_id: 11,
                tenant_id: Some(7),
            },
            1_000,
        )
        .unwrap();
        let ir = compile_turn(admitted, 1_000).unwrap();
        let mut notices = Vec::new();
        crate::transaction_ir::execute_transaction_ir_with_notices(&mut database, &ir, &mut notices)
            .unwrap();
        assert_eq!(notices.len(), 1);
        assert_eq!(notices[0].key, "fault-turn-pair");
        notices.clear();
        crate::transaction_ir::execute_transaction_ir_with_notices(&mut database, &ir, &mut notices)
            .unwrap();
        assert!(notices.is_empty());
    }

    fn assert_turn_pair_fault_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({"conversation_id": "fault-turn-pair", "user_id": 11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let queue = execute(
            &mut recovered,
            &request(
                "queue.list",
                11,
                json!({"conv_id": "fault-turn-pair", "user_id": 11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let revision = execute(
            &mut recovered,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id": "fault-turn-pair", "user_id": 11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        match turns.as_array().unwrap().len() {
            0 => {
                assert_eq!(queue, json!([]));
                assert_eq!(revision, 0);
            }
            2 => {
                assert_eq!(queue.as_array().unwrap().len(), 1);
                assert_eq!(revision, 1);
                assert_eq!(turns[1]["parentTurnId"], turns[0]["turnId"]);
                assert_eq!(queue[0]["inputTurnId"], turns[0]["turnId"]);
                assert_eq!(queue[0]["outputTurnId"], turns[1]["turnId"]);
                assert_eq!(queue[0]["attemptId"], turns[1]["currentAttemptId"]);
            }
            count => panic!("partial Turn pair recovered with {count} Turns"),
        }
    }

    #[test]
    fn every_turn_create_pair_commit_fault_recovers_one_atomic_prefix() {
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &turn_pair_fault_request(), 1_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_turn_pair_fault_recovers_atomic_prefix(baseline_vfs);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &turn_pair_fault_request(), 1_000);
            drop(database);
            assert_turn_pair_fault_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &turn_pair_fault_request(), 1_000);
            drop(database);
            assert_turn_pair_fault_recovers_atomic_prefix(vfs);
        }
    }

    #[test]
    fn passive_background_receipts_never_lease_and_do_not_block_work() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, &request("conversation.create", 11,
            json!({"conv_id":"passive-conv","user_id":11}),
            Some("passive-create")), 1_000).unwrap();
        for (id, kind, priority, message) in [
            ("legacy-result", "workflow_step", 50,
             json!({"text":"old result","_backgroundCommand":"bg_old"})),
            ("new-result", "background_receipt", 80,
             json!({"text":"new result","_backgroundCommand":"bg_new"})),
            ("real-work", "workflow_step", 50, json!({"text":"do work"})),
        ] {
            execute(&mut database, &request("queue.enqueue", 11,
                json!({"conv_id":"passive-conv","user_id":11,
                       "queue_id":id,"kind":kind,"priority":priority,
                       "message":message,"config":{},"created_at_ms":2}),
                Some(&format!("passive-enqueue-{id}"))), 1_001).unwrap();
        }
        let dequeue = |command_id: &str| request("queue.dequeue", 11,
            json!({"conv_id":"passive-conv","user_id":11,
                   "now_ms":3,"lease_ms":100}), Some(command_id));
        let work = execute(&mut database, &dequeue("passive-dequeue-work"), 1_002).unwrap();
        assert_eq!(work["queueId"], "real-work");
        execute(&mut database, &request("queue.finalize", 11,
            json!({"conv_id":"passive-conv","user_id":11,
                   "queue_id":"real-work"}), Some("passive-finish-work")),
            1_003).unwrap();
        assert!(execute(&mut database, &dequeue("passive-dequeue-empty"), 1_004).unwrap().is_null());
        let listed = execute(&mut database, &request("queue.list", 11,
            json!({"conv_id":"passive-conv","user_id":11}), None), 1_005).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 2);
        let cancelled = execute(&mut database, &request("turn.queue.cancel", 11,
            json!({"conversation_id":"passive-conv","user_id":11,
                   "queue_id":"new-result"}), Some("passive-cancel")), 1_006).unwrap();
        assert_eq!(cancelled["cancelled"], true);
        assert!(cancelled["inputTurn"].is_null());
        let remaining = execute(&mut database, &request("queue.list", 11,
            json!({"conv_id":"passive-conv","user_id":11}), None), 1_007).unwrap();
        assert_eq!(remaining.as_array().unwrap().len(), 1);
        assert_eq!(remaining[0]["queueId"], "legacy-result");

        execute(&mut database, &request("conversation.create", 11,
            json!({"conv_id":"passive-only-conv","user_id":11}),
            Some("passive-only-create")), 1_008).unwrap();
        execute(&mut database, &request("queue.enqueue", 11,
            json!({"conv_id":"passive-only-conv","user_id":11,
                   "queue_id":"passive-only-result","kind":"background_receipt",
                   "priority":80,"message":{"text":"done",
                   "_backgroundCommand":"bg_only"},"config":{},
                   "created_at_ms":2}), Some("passive-only-enqueue")), 1_009).unwrap();
        let orphaned = execute(&mut database, &request(
            "queue.conversations.list_all", 11, json!({}), None), 1_010).unwrap();
        assert!(!orphaned.as_array().unwrap().iter().any(|row|
            row["convId"] == "passive-only-conv"));
    }

    #[test]
    fn receipt_preview_and_pending_page_stay_bounded_beyond_full_response_cap() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, &request("conversation.create", 11,
            json!({"conv_id":"large-receipt-conv","user_id":11}),
            Some("large-receipt-create")), 1_000).unwrap();
        let output = "x".repeat(100_000);
        for index in 0..90 {
            let id = format!("large-receipt-{index}");
            execute(&mut database, &request("queue.enqueue", 11,
                json!({"conv_id":"large-receipt-conv","user_id":11,
                       "queue_id":id,"kind":"background_receipt","priority":80,
                       "message":{"text":output,"_backgroundCommand":id},
                       "config":{},"created_at_ms":index}),
                Some(&format!("large-receipt-enqueue-{index}"))), 1_001).unwrap();
        }
        let list = |mode: &str| request("queue.list", 11,
            json!({"conv_id":"large-receipt-conv","user_id":11,"mode":mode}), None);
        assert!(execute(&mut database, &list("full"), 1_002).is_err());
        let preview = execute(&mut database, &list("preview"), 1_003).unwrap();
        assert_eq!(preview.as_array().unwrap().len(), 90);
        assert!(preview[0].get("payload").is_none());
        let pending = execute(&mut database, &list("background_pending"), 1_004).unwrap();
        assert_eq!(pending.as_array().unwrap().len(), 32);
        assert_eq!(pending[0]["payload"]["text"], output);
    }

    #[test]
    fn turn_event_ack_removes_exact_passive_receipt_atomically() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, &request("conversation.create", 11,
            json!({"conv_id":"receipt-event-conv","user_id":11}),
            Some("receipt-event-create")), 1_000).unwrap();
        execute(&mut database, &request("turn.append_settled", 11,
            json!({"conversation_id":"receipt-event-conv","user_id":11,
                   "command_id":"receipt-event-seed","actor":"assistant",
                   "kind":"reply","status":"completed",
                   "projection":{"content":"","thinking":"","segments":[]},
                   "settlement":{"outcome":"completed","cause":"seed",
                                 "resumeOptions":[]},
                   "turn_id":"receipt-event-turn","created_at":1_100}),
            Some("receipt-event-seed-command")), 1_100).unwrap();
        let created = execute(&mut database, &request("turn.attempt.create", 11,
            json!({"conversation_id":"receipt-event-conv","user_id":11,
                   "turn_id":"receipt-event-turn","command_id":"receipt-attempt",
                   "operation":"regenerate","target_actor":"assistant",
                   "target_kind":"reply","expected_projection_revision":1,
                   "config":{}}), Some("receipt-attempt-command")), 1_150).unwrap();
        let attempt_id = created["attempt"]["attemptId"].as_str().unwrap();
        execute(&mut database, &request("turn.attempt.bind", 11,
            json!({"attempt_id":attempt_id,"task_id":"receipt-task","user_id":11}),
            Some("receipt-bind")), 1_200).unwrap();
        execute(&mut database, &request("queue.enqueue", 11,
            json!({"conv_id":"receipt-event-conv","user_id":11,
                   "queue_id":"receipt-row","kind":"background_receipt",
                   "priority":80,"message":{"text":"done",
                   "_backgroundCommand":"bg_receipt"},"config":{},
                   "created_at_ms":1_210}), Some("receipt-enqueue")), 1_210).unwrap();
        let receipt_projection = json!({
            "content":"noted", "thinking":"", "segments":[],
            "_bgCommandInjects":[{"blockId":"injection:background-command:receipt",
                "round":1,"count":1,
                "previews":[{"queueId":"receipt-row",
                             "commandId":"bg_receipt","text":"done"}]}]
        });
        let record = |command_id: &str| request("turn.event.record", 11,
            json!({"attempt_id":attempt_id,"user_id":11,
                   "task_id":"receipt-task","projection":receipt_projection.clone(),
                   "event_type":"background_command_inject",
                   "background_command_ack":{
                       "block_id":"injection:background-command:receipt",
                       "deliveries":[{"queue_id":"receipt-row",
                                      "command_id":command_id}]},
                   "now":1_300}), Some(&format!("receipt-record-{command_id}")));
        assert!(execute(&mut database, &record("wrong-command"), 1_300).is_err());
        let before = execute(&mut database, &request("queue.list", 11,
            json!({"conv_id":"receipt-event-conv","user_id":11}), None), 1_301).unwrap();
        assert_eq!(before.as_array().unwrap().len(), 1);
        let applied = execute(&mut database, &record("bg_receipt"), 1_302).unwrap();
        assert_eq!(applied["applied"], true);
        let after = execute(&mut database, &request("queue.list", 11,
            json!({"conv_id":"receipt-event-conv","user_id":11}), None), 1_303).unwrap();
        assert!(after.as_array().unwrap().is_empty());
        let events = execute(&mut database, &request("turn.events.list", 11,
            json!({"attempt_id":attempt_id,"user_id":11}), None), 1_304).unwrap();
        assert!(events.as_array().unwrap().iter().any(|event|
            event["payload"]["removedQueueIds"] == json!(["receipt-row"])));
    }

    #[test]
    fn queue_injection_ack_drains_only_never_accepted_inbox_rows() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[95; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id":"ack-conv","user_id":11,
                       "created_at":1,"updated_at":1}),
                Some("ack-conv-create"),
            ),
            1_000,
        )
        .unwrap();
        let enqueue = |queue_id: &str, kind: &str, priority: u64, message: Value| {
            request(
                "queue.enqueue",
                11,
                json!({"conv_id":"ack-conv","user_id":11,"queue_id":queue_id,
                       "kind":kind,"priority":priority,"message":message,
                       "config":{},"created_at_ms":2}),
                Some(&format!("ack-enqueue-{queue_id}")),
            )
        };
        execute(
            &mut database,
            &enqueue(
                "ack-workflow",
                "workflow_step",
                50,
                json!({"text":"bg done","_backgroundCommand":"bg_1"}),
            ),
            1_001,
        )
        .unwrap();
        execute(
            &mut database,
            &enqueue(
                "ack-peer",
                "peer_msg",
                10,
                json!({"_peerMessage":true,"_peerText":"hello","_fromConv":"other"}),
            ),
            1_002,
        )
        .unwrap();
        execute(
            &mut database,
            &enqueue(
                "ack-peer-human",
                "peer_msg",
                10,
                json!({"_peerMessage":true,"_peerHuman":true,"_peerText":"hi"}),
            ),
            1_003,
        )
        .unwrap();
        execute(
            &mut database,
            &enqueue(
                "ack-workflow-plain",
                "workflow_step",
                40,
                json!({"text":"plain"}),
            ),
            1_004,
        )
        .unwrap();
        execute(
            &mut database,
            &enqueue("ack-real", "real", 10, json!({"text":"human"})),
            1_005,
        )
        .unwrap();
        // A bound Turn-pair queue row is never an inbox drain.
        execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({"conversation_id":"ack-conv","user_id":11,
                       "command_id":"ack-pair-command",
                       "input_projection":"queued",
                       "dispatch_mode":"conversation_executor",
                       "queue_binding":{"queueId":"ack-bound","kind":"real","priority":7},
                       "conversation_defaults":{"allowCreate":false},"now":7}),
                Some("ack-pair-receipt"),
            ),
            1_006,
        )
        .unwrap();
        // A missing conversation acknowledges nothing.
        let missing = execute(
            &mut database,
            &request(
                "queue.injection.ack",
                11,
                json!({"conv_id":"ack-missing","user_id":11,"queue_ids":["ack-peer"]}),
                Some("ack-missing-1"),
            ),
            1_007,
        )
        .unwrap();
        assert_eq!(missing, json!({"removed":0,"removedQueueIds":[]}));
        // Payload validation precedes authority work.
        let mut oversized: Vec<String> = (0..=256).map(|index| format!("id-{index}")).collect();
        oversized.dedup();
        for payload in [
            json!({"conv_id":"ack-conv","user_id":11}),
            json!({"conv_id":"ack-conv","user_id":11,"queue_ids":"ack-peer"}),
            json!({"conv_id":"ack-conv","user_id":11,"queue_ids":[""]}),
            json!({"conv_id":"ack-conv","user_id":11,"queue_ids":[Value::Null]}),
            json!({"conv_id":"ack-conv","user_id":11,"queue_ids":oversized}),
        ] {
            assert_eq!(
                execute(
                    &mut database,
                    &request("queue.injection.ack", 11, payload, Some("ack-invalid")),
                    1_008,
                ),
                Err(SemanticExecutionError::InvalidPayload),
            );
        }
        let ack = execute(
            &mut database,
            &request(
                "queue.injection.ack",
                11,
                json!({"conv_id":"ack-conv","user_id":11,"queue_ids":[
                    "ack-peer","ack-workflow","ack-peer-human",
                    "ack-workflow-plain","ack-real","ack-bound","ack-peer",
                    "ack-unknown"]}),
                Some("ack-1"),
            ),
            1_009,
        )
        .unwrap();
        // Legacy (position, id) reporting order: positions stay in
        // insertion order unless a synthetic continuation is superseded.
        assert_eq!(
            ack,
            json!({"removed":2,"removedQueueIds":["ack-workflow","ack-peer"]})
        );
        let listed = execute(
            &mut database,
            &request(
                "queue.list",
                11,
                json!({"conv_id":"ack-conv","user_id":11}),
                None,
            ),
            1_010,
        )
        .unwrap();
        let remaining: Vec<&str> = listed
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["queueId"].as_str().unwrap())
            .collect();
        assert_eq!(
            remaining,
            [
                "ack-bound",
                "ack-peer-human",
                "ack-real",
                "ack-workflow-plain"
            ]
        );
        for (index, item) in listed.as_array().unwrap().iter().enumerate() {
            assert_eq!(item["position"], json!(index + 1));
        }
        // One turn.upsert change carries the drain; the header revision and
        // the bound pair stay untouched.
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id":"ack-conv","user_id":11,"after":0}),
                None,
            ),
            1_011,
        )
        .unwrap();
        let events = changes["events"].as_array().unwrap();
        assert_eq!(events.len(), 2);
        assert_eq!(events[1]["type"], "turn.upsert");
        assert_eq!(
            events[1]["payload"]["removedQueueIds"],
            json!(["ack-workflow", "ack-peer"])
        );
        assert!(events[1]["payload"].get("conversationRevision").is_none());
        let revision = execute(
            &mut database,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id":"ack-conv","user_id":11}),
                None,
            ),
            1_012,
        )
        .unwrap();
        assert_eq!(revision, json!(1));
        // Re-ack is a stable no-op publishing no further change.
        let again = execute(
            &mut database,
            &request(
                "queue.injection.ack",
                11,
                json!({"conv_id":"ack-conv","user_id":11,
                       "queue_ids":["ack-peer","ack-workflow"]}),
                Some("ack-2"),
            ),
            1_013,
        )
        .unwrap();
        assert_eq!(again, json!({"removed":0,"removedQueueIds":[]}));
        let changes = execute(
            &mut database,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id":"ack-conv","user_id":11,"after":0}),
                None,
            ),
            1_014,
        )
        .unwrap();
        assert_eq!(changes["events"].as_array().unwrap().len(), 2);
    }

    fn seeded_turn_queue_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(&mut database, &turn_pair_fault_request(), 1_000).unwrap();
        vfs.arm_fault(None).unwrap();
        database
    }

    fn turn_queue_transition_fault_request(operation: &str) -> Request {
        request(
            operation,
            11,
            json!({
                "conversation_id": "fault-turn-pair",
                "queue_id": "fault-turn-pair-queue",
                "user_id": 11
            }),
            Some(if operation == "turn.queue.activate" {
                "fault-turn-queue-activate"
            } else {
                "fault-turn-queue-cancel"
            }),
        )
    }

    fn recovered_turn_queue_state(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (Value, Value, Value, Value, Value) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"fault-turn-pair", "user_id":11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let queue = execute(
            &mut recovered,
            &request(
                "queue.list",
                11,
                json!({"conv_id":"fault-turn-pair", "user_id":11}),
                None,
            ),
            2_001,
        )
        .unwrap();
        let revision = execute(
            &mut recovered,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id":"fault-turn-pair", "user_id":11}),
                None,
            ),
            2_002,
        )
        .unwrap();
        let attempt = turns
            .as_array()
            .and_then(|turns| turns.last())
            .and_then(|turn| turn.get("currentAttemptId"))
            .and_then(Value::as_str)
            .map(|attempt_id| {
                execute(
                    &mut recovered,
                    &request(
                        "turn.attempt.get",
                        11,
                        json!({"attempt_id":attempt_id, "user_id":11}),
                        None,
                    ),
                    2_003,
                )
                .unwrap()
            })
            .unwrap_or(Value::Null);
        let changes = execute(
            &mut recovered,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id":"fault-turn-pair", "user_id":11, "after":0}),
                None,
            ),
            2_004,
        )
        .unwrap();
        (turns, queue, revision, attempt, changes)
    }

    fn assert_turn_queue_activate_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        let (turns, queue, revision, attempt, changes) = recovered_turn_queue_state(vfs);
        assert_eq!(turns.as_array().unwrap().len(), 2);
        match revision.as_u64().unwrap() {
            1 => {
                assert_eq!(queue.as_array().unwrap().len(), 1);
                assert!(attempt["queueBinding"].is_object());
                assert_eq!(changes["events"].as_array().unwrap().len(), 1);
            }
            2 => {
                assert_eq!(queue, json!([]));
                assert_eq!(attempt["queueBinding"], Value::Null);
                assert_eq!(changes["events"].as_array().unwrap().len(), 2);
                assert_eq!(
                    changes["events"][1]["payload"]["removedQueueIds"],
                    json!(["fault-turn-pair-queue"])
                );
            }
            revision => panic!("partial queue activation recovered at revision {revision}"),
        }
    }

    fn assert_turn_queue_cancel_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        let (turns, queue, revision, attempt, changes) = recovered_turn_queue_state(vfs);
        match revision.as_u64().unwrap() {
            1 => {
                assert_eq!(turns.as_array().unwrap().len(), 2);
                assert_eq!(queue.as_array().unwrap().len(), 1);
                assert!(attempt["queueBinding"].is_object());
                assert_eq!(changes["events"].as_array().unwrap().len(), 1);
            }
            2 => {
                assert_eq!(turns, json!([]));
                assert_eq!(queue, json!([]));
                assert_eq!(attempt, Value::Null);
                assert_eq!(changes["events"].as_array().unwrap().len(), 2);
                assert_eq!(
                    changes["events"][1]["payload"]["removedQueueIds"],
                    json!(["fault-turn-pair-queue"])
                );
            }
            revision => panic!("partial queue cancellation recovered at revision {revision}"),
        }
    }

    fn certify_turn_queue_transition_faults(
        operation: &str,
        assert_recovered: fn(std::sync::Arc<DeterministicVfs>),
    ) {
        let command = turn_queue_transition_fault_request(operation);
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_turn_queue_fault_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 1_100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_recovered(baseline_vfs);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_turn_queue_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_100);
            drop(database);
            assert_recovered(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_turn_queue_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_100);
            drop(database);
            assert_recovered(vfs);
        }
    }

    #[test]
    fn every_turn_queue_activate_commit_fault_recovers_one_atomic_prefix() {
        certify_turn_queue_transition_faults(
            "turn.queue.activate",
            assert_turn_queue_activate_recovers_atomic_prefix,
        );
    }

    #[test]
    fn every_turn_queue_cancel_commit_fault_recovers_one_atomic_prefix() {
        certify_turn_queue_transition_faults(
            "turn.queue.cancel",
            assert_turn_queue_cancel_recovers_atomic_prefix,
        );
    }

    fn seeded_queue_injection_ack_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> AuthorityDatabase {
        let mut database = simulated_database(vfs.clone());
        execute(
            &mut database,
            &request(
                "conversation.create",
                11,
                json!({"conv_id":"fault-ack-conv","user_id":11,
                       "created_at":1,"updated_at":1}),
                Some("fault-ack-create"),
            ),
            1_000,
        )
        .unwrap();
        for (queue_id, kind, priority, message, command) in [
            (
                "fault-ack-workflow",
                "workflow_step",
                50,
                json!({"text":"done","_backgroundCommand":"bg_1"}),
                "fault-ack-enqueue-1",
            ),
            (
                "fault-ack-peer",
                "peer_msg",
                10,
                json!({"_peerMessage":true,"_peerText":"hi"}),
                "fault-ack-enqueue-2",
            ),
            (
                "fault-ack-real",
                "real",
                10,
                json!({"text":"human"}),
                "fault-ack-enqueue-3",
            ),
        ] {
            execute(
                &mut database,
                &request(
                    "queue.enqueue",
                    11,
                    json!({"conv_id":"fault-ack-conv","user_id":11,
                           "queue_id":queue_id,"kind":kind,"priority":priority,
                           "message":message,"config":{},"created_at_ms":2}),
                    Some(command),
                ),
                1_001,
            )
            .unwrap();
        }
        vfs.arm_fault(None).unwrap();
        database
    }

    fn queue_injection_ack_fault_request() -> Request {
        request(
            "queue.injection.ack",
            11,
            json!({"conv_id":"fault-ack-conv","user_id":11,
                   "queue_ids":["fault-ack-peer","fault-ack-workflow",
                                "fault-ack-real"]}),
            Some("fault-queue-injection-ack"),
        )
    }

    fn assert_queue_injection_ack_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let queue = execute(
            &mut recovered,
            &request(
                "queue.list",
                11,
                json!({"conv_id":"fault-ack-conv","user_id":11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let changes = execute(
            &mut recovered,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id":"fault-ack-conv","user_id":11,"after":0}),
                None,
            ),
            2_001,
        )
        .unwrap();
        let events = changes["events"].as_array().unwrap();
        match queue.as_array().unwrap().len() {
            3 => assert_eq!(events.len(), 0),
            1 => {
                assert_eq!(queue.as_array().unwrap()[0]["queueId"], "fault-ack-real");
                assert_eq!(events.len(), 1);
                assert_eq!(events[0]["type"], "turn.upsert");
                assert_eq!(
                    events[0]["payload"]["removedQueueIds"],
                    json!(["fault-ack-workflow", "fault-ack-peer"])
                );
            }
            count => panic!("partial injection acknowledgement recovered with {count} queue rows"),
        }
    }

    #[test]
    fn every_queue_injection_ack_commit_fault_recovers_one_atomic_prefix() {
        let command = queue_injection_ack_fault_request();
        let baseline_vfs = prepared_vfs();
        let mut baseline = seeded_queue_injection_ack_fault_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 1_100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_queue_injection_ack_recovers_atomic_prefix(baseline_vfs);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = seeded_queue_injection_ack_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_100);
            drop(database);
            assert_queue_injection_ack_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = seeded_queue_injection_ack_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 1_100);
            drop(database);
            assert_queue_injection_ack_recovers_atomic_prefix(vfs);
        }
    }

    fn seeded_turn_steer_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String) {
        let mut database = simulated_database(vfs.clone());
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"fault-turn-steer",
                    "user_id":11,
                    "command_id":"fault-turn-steer-pair",
                    "input_projection":{"content":"question"},
                    "conversation_defaults":{"allowCreate":true},
                    "now":1_000
                }),
                Some("fault-turn-steer-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap().to_owned();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id, "task_id":"fault-steer-task", "user_id":11}),
                Some("fault-turn-steer-bind"),
            ),
            1_010,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, attempt_id)
    }

    fn turn_steer_fault_request(attempt_id: &str) -> Request {
        request(
            "turn.steer.commit",
            11,
            json!({
                "conversation_id":"fault-turn-steer",
                "attempt_id":attempt_id,
                "command_id":"fault-operator-command",
                "text":"preserve this injection atomically",
                "user_id":11
            }),
            Some("fault-turn-steer-receipt"),
        )
    }

    fn assert_turn_steer_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let output = turns.as_array().unwrap().last().unwrap();
        let revision = execute(
            &mut recovered,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            2_001,
        )
        .unwrap()
        .as_u64()
        .unwrap();
        let changes = execute(
            &mut recovered,
            &request(
                "turn.sync.changes",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11, "after":0}),
                None,
            ),
            2_002,
        )
        .unwrap();
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        match revision {
            2 => {
                assert_eq!(output["projectionRevision"], 2);
                assert!(output["projection"].get("_userSteerInjects").is_none());
                assert_eq!(changes["events"].as_array().unwrap().len(), 2);
                assert_eq!(outbox_sequence, 2);
            }
            3 => {
                assert_eq!(output["projectionRevision"], 3);
                assert_eq!(
                    output["projection"]["_userSteerInjects"][0]["blockId"],
                    "injection:user-steer:fault-operator-command"
                );
                assert_eq!(changes["events"].as_array().unwrap().len(), 3);
                assert_eq!(outbox_sequence, 3);
            }
            revision => panic!("partial Turn steer recovered at revision {revision}"),
        }
    }

    #[test]
    fn every_turn_steer_commit_fault_recovers_one_atomic_prefix() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, attempt_id) = seeded_turn_steer_fault_database(baseline_vfs.clone());
        let command = turn_steer_fault_request(&attempt_id);
        execute(&mut baseline, &command, 1_100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_turn_steer_recovers_atomic_prefix(baseline_vfs);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, attempt_id) = seeded_turn_steer_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &turn_steer_fault_request(&attempt_id), 1_100);
            drop(database);
            assert_turn_steer_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, attempt_id) = seeded_turn_steer_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &turn_steer_fault_request(&attempt_id), 1_100);
            drop(database);
            assert_turn_steer_recovers_atomic_prefix(vfs);
        }
    }

    fn seeded_turn_related_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String, String) {
        let (mut database, attempt_id) = seeded_turn_steer_fault_database(vfs.clone());
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            1_020,
        )
        .unwrap();
        let related_turn_id = turns[0]["turnId"].as_str().unwrap().to_owned();
        execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id":attempt_id,
                    "task_id":"fault-steer-task",
                    "user_id":11,
                    "slim":true,
                    "content":"x".repeat(100 * 1024),
                    "thinking":"",
                    "now":1_030
                }),
                Some("fault-turn-related-live-head"),
            ),
            1_030,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, attempt_id, related_turn_id)
    }

    fn turn_related_fault_request(attempt_id: &str, related_turn_id: &str) -> Request {
        request(
            "turn.related.announce",
            11,
            json!({
                "attempt_id":attempt_id,
                "turn_ids":[related_turn_id],
                "user_id":11
            }),
            Some("fault-turn-related-command"),
        )
    }

    fn assert_turn_related_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let root = turns.as_array().unwrap().last().unwrap();
        let attempt_id = root["currentAttemptId"].as_str().unwrap();
        let revision = execute(
            &mut recovered,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            2_001,
        )
        .unwrap()
        .as_u64()
        .unwrap();
        let events = execute(
            &mut recovered,
            &request(
                "turn.events.list",
                11,
                json!({"attempt_id":attempt_id, "user_id":11, "projection_mode":"patch"}),
                None,
            ),
            2_002,
        )
        .unwrap();
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        match revision {
            3 => {
                assert_eq!(root["projectionRevision"], 3);
                assert_eq!(events.as_array().unwrap().len(), 3);
                assert_eq!(outbox_sequence, 3);
            }
            4 => {
                assert_eq!(root["projectionRevision"], 4);
                assert_eq!(events.as_array().unwrap().len(), 4);
                assert_eq!(events[3]["payload"]["updateKind"], "related_turns_created");
                assert_eq!(outbox_sequence, 4);
            }
            revision => panic!("partial related Turn announce recovered at revision {revision}"),
        }
    }

    #[test]
    fn every_turn_related_announce_fault_recovers_one_atomic_prefix() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, attempt_id, turn_id) =
            seeded_turn_related_fault_database(baseline_vfs.clone());
        let command = turn_related_fault_request(&attempt_id, &turn_id);
        execute(&mut baseline, &command, 1_100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_turn_related_recovers_atomic_prefix(baseline_vfs);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, attempt_id, turn_id) =
                seeded_turn_related_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &turn_related_fault_request(&attempt_id, &turn_id),
                1_100,
            );
            drop(database);
            assert_turn_related_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, attempt_id, turn_id) =
                seeded_turn_related_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &turn_related_fault_request(&attempt_id, &turn_id),
                1_100,
            );
            drop(database);
            assert_turn_related_recovers_atomic_prefix(vfs);
        }
    }

    fn seeded_turn_visible_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, String, String) {
        let (mut database, attempt_id) = seeded_turn_steer_fault_database(vfs.clone());
        let turns = execute(
            &mut database,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            1_020,
        )
        .unwrap();
        let root_turn_id = turns.as_array().unwrap().last().unwrap()["turnId"]
            .as_str()
            .unwrap()
            .to_owned();
        execute(
            &mut database,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id":attempt_id,
                    "task_id":"fault-steer-task",
                    "user_id":11,
                    "content":"live",
                    "thinking":"",
                    "now":1_030
                }),
                Some("fault-turn-visible-live-head"),
            ),
            1_030,
        )
        .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, attempt_id, root_turn_id)
    }

    fn turn_visible_fault_request(attempt_id: &str, root_turn_id: &str) -> Request {
        request(
            "turn.visible.sync",
            11,
            json!({
                "conversation_id":"fault-turn-steer",
                "attempt_id":attempt_id,
                "root_turn_id":root_turn_id,
                "default_kind":"flow_node",
                "run_id":"fault-visible-run",
                "user_id":11,
                "messages":[
                    {"role":"assistant", "_isFlowPlanner":true, "content":"plan"},
                    {"role":"user", "_isFlowReview":true, "_flowApproved":false,
                     "content":"revise"}
                ]
            }),
            Some("fault-turn-visible-command"),
        )
    }

    fn assert_turn_visible_recovers_atomic_prefix(vfs: std::sync::Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let turns = execute(
            &mut recovered,
            &request(
                "turn.list",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            2_000,
        )
        .unwrap();
        let root = &turns[1];
        let attempt_id = root["currentAttemptId"].as_str().unwrap();
        let revision = execute(
            &mut recovered,
            &request(
                "turn.revision",
                11,
                json!({"conversation_id":"fault-turn-steer", "user_id":11}),
                None,
            ),
            2_001,
        )
        .unwrap()
        .as_u64()
        .unwrap();
        let events = execute(
            &mut recovered,
            &request(
                "turn.events.list",
                11,
                json!({"attempt_id":attempt_id, "user_id":11, "projection_mode":"patch"}),
                None,
            ),
            2_002,
        )
        .unwrap();
        let outbox_sequence = recovered
            .logical_outbox_status(7, 11)
            .unwrap()
            .last_sequence;
        match revision {
            3 => {
                assert_eq!(turns.as_array().unwrap().len(), 2);
                assert_eq!(root["projectionRevision"], 3);
                assert_eq!(events.as_array().unwrap().len(), 3);
                assert_eq!(outbox_sequence, 3);
            }
            4 => {
                assert_eq!(turns.as_array().unwrap().len(), 3);
                assert_eq!(root["projectionRevision"], 4);
                assert_eq!(root["actor"], "planner");
                assert_eq!(events.as_array().unwrap().len(), 4);
                assert_eq!(
                    events[3]["payload"]["updateKind"],
                    "visible_turns_committed"
                );
                assert_eq!(events[3]["payload"]["turns"].as_array().unwrap().len(), 1);
                assert_eq!(outbox_sequence, 4);
            }
            revision => panic!("partial visible Turn sync recovered at revision {revision}"),
        }
    }

    #[test]
    fn every_turn_visible_sync_fault_recovers_one_atomic_prefix() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, attempt_id, root_turn_id) =
            seeded_turn_visible_fault_database(baseline_vfs.clone());
        let command = turn_visible_fault_request(&attempt_id, &root_turn_id);
        execute(&mut baseline, &command, 1_100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_turn_visible_recovers_atomic_prefix(baseline_vfs);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, attempt_id, root_turn_id) =
                seeded_turn_visible_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &turn_visible_fault_request(&attempt_id, &root_turn_id),
                1_100,
            );
            drop(database);
            assert_turn_visible_recovers_atomic_prefix(vfs);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, attempt_id, root_turn_id) =
                seeded_turn_visible_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &turn_visible_fault_request(&attempt_id, &root_turn_id),
                1_100,
            );
            drop(database);
            assert_turn_visible_recovers_atomic_prefix(vfs);
        }
    }

    fn paper_podcast_upsert_command(owner: u64, command_id: &str) -> Request {
        request(
            "paper.podcast.upsert",
            owner,
            json!({
                "user_id": owner,
                "paper_hash": "podcast-fault-paper",
                "mode": "short",
                "lang": "zh",
                "voice": "alloy",
                "status": "generating",
                "script": {"segments": [{"text": "s".repeat(300_000)}]},
                "meta": {"task_id": "podcast-task"},
                "duration_sec": 0,
                "created_at": 100,
                "updated_at": 100,
            }),
            Some(command_id),
        )
    }

    #[test]
    fn paper_podcasts_preserve_creation_and_interrupt_all_tenant_owners_boundedly() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[93; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        for owner in [11, 12] {
            execute(
                &mut database,
                &paper_podcast_upsert_command(owner, &format!("podcast-{owner}")),
                2_000,
            )
            .unwrap();
        }
        let interrupted = request(
            "paper.podcast.mark_interrupted",
            11,
            json!({"updated_at": 200}),
            Some("podcast-interrupt"),
        );
        assert_eq!(
            execute(&mut database, &interrupted, 2_001).unwrap(),
            json!({"changed": 2})
        );
        assert_eq!(
            execute(&mut database, &interrupted, 2_002).unwrap(),
            json!({"changed": 2})
        );
        for owner in [11, 12] {
            let value = execute(
                &mut database,
                &request(
                    "paper.podcast.get",
                    owner,
                    json!({
                        "user_id": owner, "paper_hash": "podcast-fault-paper",
                        "mode": "short", "lang": "zh", "voice": "alloy",
                    }),
                    None,
                ),
                2_003,
            )
            .unwrap();
            assert_eq!(value["status"], "interrupted");
            assert_eq!(value["updated_at"], 200);
            assert_eq!(value["created_at"], 100);
        }
        let completed = request(
            "paper.podcast.upsert",
            11,
            json!({
                "user_id": 11, "paper_hash": "podcast-fault-paper",
                "mode": "short", "lang": "zh", "voice": "alloy",
                "status": "done", "script": {"segments": [{"text": "complete"}]},
                "meta": {}, "duration_sec": 3.5,
                "created_at": 999, "updated_at": 300,
            }),
            Some("podcast-complete"),
        );
        execute(&mut database, &completed, 2_004).unwrap();
        let value = execute(
            &mut database,
            &request(
                "paper.podcast.get",
                11,
                json!({
                    "user_id": 11, "paper_hash": "podcast-fault-paper",
                    "mode": "short", "lang": "zh", "voice": "alloy",
                }),
                None,
            ),
            2_005,
        )
        .unwrap();
        assert_eq!(value["created_at"], 100);
        assert_eq!(value["duration_sec"], 3.5);
    }

    fn assert_paper_podcast_fault_replay(vfs: std::sync::Arc<DeterministicVfs>, command: &Request) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[71; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let value = execute(
            &mut recovered,
            &request(
                "paper.podcast.get",
                11,
                json!({
                    "user_id": 11, "paper_hash": "podcast-fault-paper",
                    "mode": "short", "lang": "zh", "voice": "alloy",
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(value["status"], "generating");
        assert_eq!(
            value["script_json"]["segments"][0]["text"]
                .as_str()
                .unwrap()
                .len(),
            300_000
        );
    }

    #[test]
    fn every_paper_podcast_upsert_fault_recovers_documents_active_index_counts_and_receipt() {
        let command = paper_podcast_upsert_command(11, "podcast-fault");
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_podcast_fault_replay(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_podcast_fault_replay(vfs, &command);
        }
    }

    fn small_paper_podcast_upsert_command(owner: u64, command_id: &str) -> Request {
        request(
            "paper.podcast.upsert",
            owner,
            json!({
                "user_id": owner, "paper_hash": "podcast-interrupt-fault",
                "mode": "short", "lang": "en", "voice": "",
                "status": "generating", "script": {}, "meta": {},
                "duration_sec": 0, "created_at": 10, "updated_at": 10,
            }),
            Some(command_id),
        )
    }

    fn seeded_podcast_interruption_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, usize) {
        let mut database = simulated_database(vfs.clone());
        for owner in [11, 12] {
            execute(
                &mut database,
                &small_paper_podcast_upsert_command(
                    owner,
                    &format!("podcast-interrupt-seed-{owner}"),
                ),
                1_000 + owner,
            )
            .unwrap();
        }
        let trace_start = vfs.trace().unwrap().len();
        (database, trace_start)
    }

    fn assert_paper_podcast_interruption_fault_replay(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(&mut recovered, command, 3_000).unwrap(),
            json!({"changed": 2})
        );
        for owner in [11, 12] {
            let value = execute(
                &mut recovered,
                &request(
                    "paper.podcast.get",
                    owner,
                    json!({
                        "user_id": owner, "paper_hash": "podcast-interrupt-fault",
                        "mode": "short", "lang": "en", "voice": "",
                    }),
                    None,
                ),
                3_001,
            )
            .unwrap();
            assert_eq!(value["status"], "interrupted");
        }
    }

    #[test]
    fn every_paper_podcast_interruption_fault_recovers_all_owner_states_and_global_index() {
        let command = request(
            "paper.podcast.mark_interrupted",
            11,
            json!({"updated_at": 20}),
            Some("podcast-interrupt-fault"),
        );
        let baseline_vfs = prepared_vfs();
        let (mut baseline, trace_start) =
            seeded_podcast_interruption_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        let interruption_trace = &trace[trace_start..];
        for relative_index in 0..interruption_trace.len() {
            let vfs = prepared_vfs();
            let (mut database, actual_start) = seeded_podcast_interruption_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: (actual_start + relative_index + 1) as u64,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_podcast_interruption_fault_replay(vfs, &command);
        }
        for (relative_index, operation) in interruption_trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, actual_start) = seeded_podcast_interruption_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: (actual_start + relative_index + 1) as u64,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_podcast_interruption_fault_replay(vfs, &command);
        }
    }

    fn paper_report_upsert_command(command_id: &str) -> Request {
        request(
            "paper.report.upsert",
            11,
            json!({
                "user_id": 11, "paper_hash": "report-fault-paper", "lang": "en",
                "report": "r".repeat(300_000), "model": "model",
                "meta": {"promptTokens": 7}, "created_at": 10,
            }),
            Some(command_id),
        )
    }

    #[test]
    fn paper_reports_and_translations_are_split_bounded_and_reopenable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &paper_report_upsert_command("report-put"),
                2_000
            )
            .unwrap(),
            json!({"saved": true})
        );
        let merge = request(
            "paper.report.second_pass.accumulate",
            11,
            json!({
                "user_id": 11, "paper_hash": "report-fault-paper", "lang": "en", "name": "review",
                "usage": {"prompt_tokens": 3}, "costCny": 0.5,
            }),
            Some("report-pass"),
        );
        let merged = execute(&mut database, &merge, 2_001).unwrap();
        assert_eq!(merged["meta"]["totalUsage"]["prompt_tokens"], 10);
        assert_eq!(merged["meta"]["totalCostCny"], 0.5);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "paper.translation.upsert",
                    11,
                    json!({
                        "user_id": 11, "paper_hash": "report-fault-paper", "lang": "zh",
                        "text": "translated", "model": "translator", "created_at": 20,
                    }),
                    Some("translation-put")
                ),
                2_002
            )
            .unwrap(),
            json!({"saved": true})
        );
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let bounded = execute(&mut reopened, &request("paper.report.get", 11, json!({
            "user_id": 11, "paper_hash": "report-fault-paper", "lang": "en", "max_report_chars": 4,
        }), None), 3_000).unwrap();
        assert_eq!(bounded["report"], "rrrr");
        assert_eq!(bounded["model"], "");
        assert_eq!(bounded["meta"], json!({}));
        let translation = execute(
            &mut reopened,
            &request(
                "paper.translation.get",
                11,
                json!({
                    "user_id": 11, "paper_hash": "report-fault-paper", "lang": "zh",
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(translation["text"], "translated");
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            3
        );
    }

    fn assert_paper_report_fault_replay(vfs: std::sync::Arc<DeterministicVfs>, command: &Request) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let report = execute(
            &mut recovered,
            &request(
                "paper.report.get",
                11,
                json!({
                    "user_id": 11, "paper_hash": "report-fault-paper", "lang": "en",
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(report["report"].as_str().unwrap().len(), 300_000);
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    #[test]
    fn every_paper_report_upsert_fault_recovers_core_meta_index_count_receipt_and_outbox() {
        let command = paper_report_upsert_command("paper-report-fault");
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_report_fault_replay(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_report_fault_replay(vfs, &command);
        }
    }

    fn paper_library_put_command(command_id: &str) -> Request {
        request(
            "paper.library.put",
            11,
            json!({
                "user_id": 11,
                "id": "library-primary",
                "title": "arXiv: 2401.00001",
                "pdf_url": "https://example.invalid/paper.pdf",
                "pdf_filename": "paper.pdf",
                "arxiv_id": "2401.00001",
                "paper_hash": "paper-library-hash",
                "parsed_text": "abcdef",
                "parser_version": "parser-v1",
                "qa_history": "[]",
                "images": "[]",
                "babel_cache": "{}",
                "page_count": 4,
                "folder_id": "inbox",
                "created_at": 10,
                "updated_at": 20,
            }),
            Some(command_id),
        )
    }

    #[test]
    fn paper_library_is_projected_repaired_owner_scoped_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let put = paper_library_put_command("paper-library-put");
        assert_eq!(
            execute(&mut database, &put, 2_000).unwrap(),
            json!({"saved": true})
        );
        assert_eq!(
            execute(&mut database, &put, 2_001).unwrap(),
            json!({"saved": true})
        );
        let inputs = execute(
            &mut database,
            &request(
                "paper.library.inputs",
                11,
                json!({
                    "user_id": 11,
                    "arxiv_ids": [" 2401.00001 ", "2401.00001"],
                    "max_text_chars": 3,
                }),
                None,
            ),
            2_002,
        )
        .unwrap();
        assert_eq!(inputs.as_array().unwrap().len(), 1);
        assert_eq!(inputs[0]["parsedText"], "abc");
        assert_eq!(inputs[0]["parsedTextLength"], 6);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "paper.library.title.backfill",
                    11,
                    json!({
                        "user_id": 11,
                        "paper_hash": "paper-library-hash",
                        "title": "Recovered title",
                    }),
                    Some("paper-library-title"),
                ),
                3_000,
            )
            .unwrap(),
            json!({"title": "Recovered title", "updated": 1})
        );
        let identity = execute(
            &mut database,
            &request(
                "paper.library.identity",
                11,
                json!({
                    "user_id": 11,
                    "paper_hash": "paper-library-hash",
                    "max_text_chars": 0,
                    "include_text_length": false,
                }),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(identity["title"], "Recovered title");
        assert_eq!(identity["parsed_text"], "");
        assert_eq!(identity["parsed_text_length"], 0);
        assert_eq!(
            execute(
                &mut database,
                &request("paper.library.summaries", 12, json!({"user_id": 12}), None),
                3_002,
            )
            .unwrap(),
            json!([])
        );
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let get = execute(
            &mut reopened,
            &request(
                "paper.library.reader",
                11,
                json!({"user_id": 11, "id": "library-primary"}),
                None,
            ),
            4_000,
        )
        .unwrap();
        assert_eq!(get["title"], "Recovered title");
        assert!(get.get("babelCache").is_none());
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            2
        );
    }

    fn assert_paper_library_fault_replay(vfs: std::sync::Arc<DeterministicVfs>, command: &Request) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let listed = execute(
            &mut recovered,
            &request("paper.library.summaries", 11, json!({"user_id": 11}), None),
            3_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["id"], "library-primary");
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    #[test]
    fn every_paper_library_put_fault_recovers_documents_indexes_count_receipt_and_outbox() {
        let command = paper_library_put_command("paper-library-fault");
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_library_fault_replay(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_library_fault_replay(vfs, &command);
        }
    }

    fn paper_note_create_command(command_id: &str) -> Request {
        request(
            "paper.note.create",
            11,
            json!({
                "user_id": 11,
                "id": "note-primary",
                "paper_hash": "paper-notes",
                "lang": "en",
                "anchor": {"page": 3, "quote": "bounded"},
                "note": "n".repeat(100_000),
                "created_at": 10,
                "updated_at": 11,
            }),
            Some(command_id),
        )
    }

    fn zlib_base64(raw: &[u8]) -> String {
        use std::io::Write as _;

        let mut encoder =
            flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
        encoder.write_all(raw).unwrap();
        base64::engine::general_purpose::STANDARD.encode(encoder.finish().unwrap())
    }

    #[test]
    fn raw_archives_are_parent_fenced_quota_bounded_lazy_and_owner_scoped() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[93; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"raw-conversation",
                    "user_id":11,
                    "command_id":"raw-pair",
                    "input_projection":{"content":"capture"},
                    "conversation_defaults":{"allowCreate":true},
                    "now":1_000
                }),
                Some("raw-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let turn_id = pair["attempt"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id,"task_id":"raw-task","user_id":11}),
                Some("raw-bind"),
            ),
            1_001,
        )
        .unwrap();
        let request_body = b"provider request";
        let response_body = b"data: first\n\ndata: second\n\n";
        let payload = json!({
            "archive_id":"raw-primary","conversation_id":"raw-conversation",
            "turn_id":turn_id,"attempt_id":attempt_id,"task_id":"raw-task",
            "user_id":11,"round_num":1,"transport_attempt":0,
            "request_blob_b64":zlib_base64(request_body),
            "response_blob_b64":zlib_base64(response_body),
            "request_bytes":request_body.len(),"response_bytes":response_body.len(),
            "request_sha256":format!("{:x}",Sha256::digest(request_body)),
            "response_sha256":format!("{:x}",Sha256::digest(response_body)),
            "integrity":"complete","truncation_reason":"",
            "summary":{"text":"Provider request/response"},
            "budget_bytes":1_048_576,"min_free_bytes":0,"available_free_bytes":0
        });
        let stored = execute(
            &mut database,
            &request("raw_archive.put", 11, payload.clone(), Some("raw-put")),
            1_002,
        )
        .unwrap();
        assert_eq!(stored["integrity"], "complete");
        assert_eq!(stored["requestAvailable"], true);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "raw_archive.put",
                    11,
                    payload.clone(),
                    Some("raw-put-replay"),
                ),
                1_003,
            )
            .unwrap()["idempotentReplay"],
            true
        );
        let listed = execute(
            &mut database,
            &request(
                "raw_archive.list",
                11,
                json!({"user_id":11,"task_id":"raw-task","round_num":1}),
                None,
            ),
            1_004,
        )
        .unwrap();
        assert_eq!(listed["archives"].as_array().unwrap().len(), 1);
        let read = execute(
            &mut database,
            &request(
                "raw_archive.read",
                11,
                json!({"user_id":11,"task_id":"raw-task","archive_id":"raw-primary","part":"response","offset":0,"limit":16}),
                None,
            ),
            1_005,
        )
        .unwrap();
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(read["dataBase64"].as_str().unwrap())
                .unwrap(),
            response_body[..16]
        );
        assert_eq!(read["hasMore"], true);
        let mut foreign_payload = payload.clone();
        foreign_payload["user_id"] = Value::from(12);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "raw_archive.put",
                    12,
                    foreign_payload,
                    Some("raw-foreign-put"),
                ),
                1_006,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
        let mut quota_payload = payload;
        quota_payload["archive_id"] = Value::String("raw-quota".to_owned());
        quota_payload["available_free_bytes"] = Value::from(1);
        let quota = execute(
            &mut database,
            &request("raw_archive.put", 11, quota_payload, Some("raw-quota-put")),
            1_007,
        )
        .unwrap();
        assert_eq!(quota["integrity"], "partial");
        assert_eq!(quota["truncationReason"], "quota_exhausted");
        assert_eq!(quota["storedBytes"], 0);
        assert_eq!(quota["requestAvailable"], false);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "raw_archive.list",
                    12,
                    json!({"user_id":12,"task_id":"raw-task"}),
                    None,
                ),
                1_008,
            )
            .unwrap(),
            json!({"archives":[]})
        );
        execute(
            &mut database,
            &request(
                "conversation.delete",
                11,
                json!({"conv_id":"raw-conversation","user_id":11}),
                Some("raw-conversation-delete"),
            ),
            1_009,
        )
        .unwrap();
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "raw_archive.list",
                    11,
                    json!({"user_id":11,"task_id":"raw-task"}),
                    None,
                ),
                1_010,
            )
            .unwrap(),
            json!({"archives":[]})
        );
        let replacement = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"raw-conversation-reused",
                    "user_id":11,
                    "command_id":"raw-pair-reused",
                    "input_projection":{"content":"capture again"},
                    "conversation_defaults":{"allowCreate":true},
                    "now":1_011
                }),
                Some("raw-pair-reused-receipt"),
            ),
            1_011,
        )
        .unwrap();
        let replacement_attempt = replacement["attempt"]["attemptId"].as_str().unwrap();
        let replacement_turn = replacement["attempt"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":replacement_attempt,"task_id":"raw-task","user_id":11}),
                Some("raw-bind-reused"),
            ),
            1_012,
        )
        .unwrap();
        let replacement_body = b"replacement";
        let replacement_archive = execute(
            &mut database,
            &request(
                "raw_archive.put",
                11,
                json!({
                    "archive_id":"raw-primary","conversation_id":"raw-conversation-reused",
                    "turn_id":replacement_turn,"attempt_id":replacement_attempt,"task_id":"raw-task",
                    "user_id":11,"round_num":1,"transport_attempt":0,
                    "request_blob_b64":zlib_base64(replacement_body),"response_blob_b64":zlib_base64(replacement_body),
                    "request_bytes":replacement_body.len(),"response_bytes":replacement_body.len(),
                    "request_sha256":format!("{:x}",Sha256::digest(replacement_body)),
                    "response_sha256":format!("{:x}",Sha256::digest(replacement_body)),
                    "integrity":"complete","summary":{},"budget_bytes":1_048_576
                }),
                Some("raw-reused-put"),
            ),
            1_013,
        )
        .unwrap();
        assert_eq!(replacement_archive["idempotentReplay"], false);
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap().last_sequence,
            8
        );
    }

    fn seeded_raw_archive_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, Request) {
        let mut database = simulated_database(vfs.clone());
        database
            .configure_logical_outbox(&[93; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let pair = execute(
            &mut database,
            &request(
                "turn.create_pair",
                11,
                json!({
                    "conversation_id":"raw-fault-conversation","user_id":11,
                    "command_id":"raw-fault-pair","input_projection":{"content":"capture"},
                    "conversation_defaults":{"allowCreate":true},"now":1_000
                }),
                Some("raw-fault-pair-receipt"),
            ),
            1_000,
        )
        .unwrap();
        let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
        let turn_id = pair["attempt"]["turnId"].as_str().unwrap();
        execute(
            &mut database,
            &request(
                "turn.attempt.bind",
                11,
                json!({"attempt_id":attempt_id,"task_id":"raw-fault-task","user_id":11}),
                Some("raw-fault-bind"),
            ),
            1_001,
        )
        .unwrap();
        let body = b"data: fault-safe\n\n";
        let command = request(
            "raw_archive.put",
            11,
            json!({
                "archive_id":"raw-fault","conversation_id":"raw-fault-conversation",
                "turn_id":turn_id,"attempt_id":attempt_id,"task_id":"raw-fault-task",
                "user_id":11,"round_num":1,"transport_attempt":0,
                "request_blob_b64":zlib_base64(b"request"),
                "response_blob_b64":zlib_base64(body),
                "request_bytes":7,"response_bytes":body.len(),
                "request_sha256":format!("{:x}",Sha256::digest(b"request")),
                "response_sha256":format!("{:x}",Sha256::digest(body)),
                "integrity":"complete","truncation_reason":"","summary":{},
                "budget_bytes":1_048_576,"min_free_bytes":0,"available_free_bytes":0
            }),
            Some("raw-fault-put"),
        );
        vfs.arm_fault(None).unwrap();
        (database, command)
    }

    fn assert_raw_archive_fault_replay(vfs: std::sync::Arc<DeterministicVfs>, command: &Request) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[93; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let listed = execute(
            &mut recovered,
            &request(
                "raw_archive.list",
                11,
                json!({"user_id":11,"task_id":"raw-fault-task"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed["archives"].as_array().unwrap().len(), 1);
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            3
        );
    }

    #[test]
    fn every_raw_archive_put_fault_recovers_document_indexes_usage_and_outbox_atomically() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, command) = seeded_raw_archive_fault_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_raw_archive_fault_replay(baseline_vfs, &command);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, command) = seeded_raw_archive_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_raw_archive_fault_replay(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, command) = seeded_raw_archive_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_raw_archive_fault_replay(vfs, &command);
        }
    }

    fn seeded_raw_archive_delete_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, Request) {
        let (mut database, put) = seeded_raw_archive_fault_database(vfs.clone());
        execute(&mut database, &put, 2_000).unwrap();
        let delete = request(
            "conversation.delete",
            11,
            json!({"conv_id":"raw-fault-conversation","user_id":11}),
            Some("raw-fault-delete"),
        );
        vfs.arm_fault(None).unwrap();
        (database, delete)
    }

    fn assert_raw_archive_delete_fault_replay(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[93; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let listed = execute(
            &mut recovered,
            &request(
                "raw_archive.list",
                11,
                json!({"user_id":11,"task_id":"raw-fault-task"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed, json!({"archives":[]}));
        assert_eq!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            4
        );
    }

    #[test]
    fn every_raw_archive_delete_fault_recovers_indexes_usage_and_lifecycle_atomically() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, command) =
            seeded_raw_archive_delete_fault_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_100).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_raw_archive_delete_fault_replay(baseline_vfs, &command);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, command) = seeded_raw_archive_delete_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_100);
            drop(database);
            assert_raw_archive_delete_fault_replay(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, command) = seeded_raw_archive_delete_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_100);
            drop(database);
            assert_raw_archive_delete_fault_replay(vfs, &command);
        }
    }

    #[test]
    fn paper_notes_are_ordered_blob_bounded_owner_scoped_and_receipted() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let create = paper_note_create_command("paper-note-create");
        assert_eq!(
            execute(&mut database, &create, 2_000).unwrap(),
            json!({"saved": true})
        );
        assert_eq!(
            execute(&mut database, &create, 2_001).unwrap(),
            json!({"saved": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "paper.note.create",
                    11,
                    json!({
                        "user_id": 11, "id": "note-first", "paper_hash": "paper-notes",
                        "lang": "en", "anchor": {}, "note": "first",
                        "created_at": 9, "updated_at": 9,
                    }),
                    Some("paper-note-first"),
                ),
                2_002,
            )
            .unwrap(),
            json!({"saved": true})
        );
        let list_request = request(
            "paper.note.list",
            11,
            json!({"user_id": 11, "paper_hash": "paper-notes", "lang": "en"}),
            None,
        );
        let listed = execute(&mut database, &list_request, 2_003).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 2);
        assert_eq!(listed[0]["id"], "note-first");
        assert_eq!(listed[1]["note"].as_str().unwrap().len(), 100_000);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "paper.note.update",
                    11,
                    json!({"user_id": 11, "id": "note-primary", "note": "updated", "updated_at": 12}),
                    Some("paper-note-update"),
                ),
                2_004,
            )
            .unwrap(),
            json!({"updated": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "paper.note.list",
                    12,
                    json!({"user_id": 12, "paper_hash": "paper-notes", "lang": "en"}),
                    None,
                ),
                2_005,
            )
            .unwrap(),
            json!([])
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "paper.note.delete",
                    11,
                    json!({"user_id": 11, "id": "note-first"}),
                    Some("paper-note-delete"),
                ),
                2_006,
            )
            .unwrap(),
            json!({"deleted": true})
        );
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[94; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let listed = execute(&mut reopened, &list_request, 3_000).unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["note"], "updated");
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            4
        );
    }

    fn assert_paper_note_fault_replay(vfs: std::sync::Arc<DeterministicVfs>, command: &Request) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let listed = execute(
            &mut recovered,
            &request(
                "paper.note.list",
                11,
                json!({"user_id": 11, "paper_hash": "paper-notes", "lang": "en"}),
                None,
            ),
            3_001,
        )
        .unwrap();
        assert_eq!(listed.as_array().unwrap().len(), 1);
        assert_eq!(listed[0]["note"].as_str().unwrap().len(), 100_000);
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    #[test]
    fn every_paper_note_create_fault_recovers_document_index_count_receipt_and_outbox() {
        let command = paper_note_create_command("paper-note-fault");
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, &command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_note_fault_replay(vfs, &command);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, &command, 2_000);
            drop(database);
            assert_paper_note_fault_replay(vfs, &command);
        }
    }

    fn research_artifact_command(command_id: &str) -> Request {
        request(
            "research.artifact.upsert",
            11,
            json!({
                "user_id": 11,
                "paper_hash": "research-paper",
                "lang_key": "survey:en",
                "report": "x".repeat(300_000),
                "model": "research-model",
                "meta": {
                    "kind": "survey",
                    "direction": "bounded storage",
                    "open_gaps": {"open_gaps": []}
                },
                "created_at": 1_000
            }),
            Some(command_id),
        )
    }

    fn research_workspace_command(command_id: &str) -> Request {
        request(
            "research.workspace.put",
            11,
            json!({
                "user_id": 11,
                "paper_hash": "research-paper",
                "lang": "en",
                "expected_revision": 0,
                "updated_at": 0,
                "workspace": {"direction": "bounded storage", "notes": "y".repeat(300_000)}
            }),
            Some(command_id),
        )
    }

    #[test]
    fn research_artifacts_and_workspaces_are_blob_bounded_indexed_and_owner_scoped() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[91; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let survey = research_artifact_command("research-survey");
        assert_eq!(
            execute(&mut database, &survey, 2_000).unwrap(),
            json!({"saved": true})
        );
        assert_eq!(
            execute(&mut database, &survey, 2_001).unwrap(),
            json!({"saved": true})
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "research.artifact.upsert",
                    11,
                    json!({
                        "user_id": 11,
                        "paper_hash": "research-paper",
                        "lang_key": "ideate:en",
                        "report": "ideas",
                        "model": "research-model",
                        "meta": {
                            "kind": "ideate",
                            "direction": "bounded storage",
                            "accepted": [{"id": "accepted"}],
                            "rejected": [{"id": "rejected"}],
                            "gate_reached": "accepted",
                            "degraded": true
                        },
                        "created_at": 1_001
                    }),
                    Some("research-ideate"),
                ),
                2_002,
            )
            .unwrap(),
            json!({"saved": true})
        );
        let artifacts = execute(
            &mut database,
            &request(
                "research.artifacts.get",
                11,
                json!({"user_id": 11, "paper_hash": "research-paper", "lang": "en"}),
                None,
            ),
            2_003,
        )
        .unwrap();
        assert_eq!(artifacts.as_array().unwrap().len(), 2);
        assert_eq!(artifacts[0]["lang_key"], "ideate:en");
        assert_eq!(artifacts[1]["report"].as_str().unwrap().len(), 300_000);
        let directions = execute(
            &mut database,
            &request(
                "research.directions.list",
                11,
                json!({"user_id": 11, "limit": 50}),
                None,
            ),
            2_004,
        )
        .unwrap();
        assert_eq!(directions.as_array().unwrap().len(), 1);
        assert_eq!(directions[0]["direction"], "bounded storage");
        assert_eq!(directions[0]["accepted"], 1);
        assert_eq!(directions[0]["rejected"], 1);
        assert_eq!(directions[0]["has_survey"], true);
        assert_eq!(directions[0]["has_ideas"], true);

        let workspace = research_workspace_command("research-workspace-1");
        let first = execute(&mut database, &workspace, 3_000).unwrap();
        assert_eq!(first["revision"], 1);
        assert_eq!(first["updated_at_ms"], 0);
        assert_eq!(first["workspace"]["notes"].as_str().unwrap().len(), 300_000);
        assert_eq!(execute(&mut database, &workspace, 3_001).unwrap(), first);
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "research.workspace.put",
                    11,
                    json!({
                        "user_id": 11, "paper_hash": "research-paper", "lang": "en",
                        "expected_revision": 0, "updated_at": 2, "workspace": {}
                    }),
                    Some("research-workspace-stale"),
                ),
                3_002,
            )
            .unwrap_err(),
            SemanticExecutionError::Conflict
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "research.workspace.get",
                    12,
                    json!({"user_id": 12, "paper_hash": "research-paper", "lang": "en"}),
                    None,
                ),
                3_003,
            )
            .unwrap(),
            Value::Null
        );
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[91; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        let persisted = execute(
            &mut reopened,
            &request(
                "research.workspace.get",
                11,
                json!({"user_id": 11, "paper_hash": "research-paper", "lang": "en"}),
                None,
            ),
            4_000,
        )
        .unwrap();
        assert_eq!(persisted, first);
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            3
        );
    }

    fn assert_research_fault_replay(
        vfs: std::sync::Arc<DeterministicVfs>,
        command: &Request,
        read: &Request,
        expected_field: &str,
    ) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 128 * 1024)
            .unwrap();
        execute(&mut recovered, command, 3_000).unwrap();
        let stored = execute(&mut recovered, read, 3_001).unwrap();
        if expected_field.is_empty() {
            assert!(!stored.as_array().unwrap().is_empty());
        } else {
            assert!(!stored[expected_field].is_null());
        }
        assert!(matches!(
            recovered
                .logical_outbox_status(7, 11)
                .unwrap()
                .last_sequence,
            1 | 2
        ));
    }

    fn run_research_fault_matrix(command: &Request, read: &Request, expected_field: &str) {
        let baseline_vfs = prepared_vfs();
        let mut baseline = simulated_database(baseline_vfs.clone());
        execute(&mut baseline, command, 2_000).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(&mut database, command, 2_000);
            drop(database);
            assert_research_fault_replay(vfs, command, read, expected_field);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let mut database = simulated_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(&mut database, command, 2_000);
            drop(database);
            assert_research_fault_replay(vfs, command, read, expected_field);
        }
    }

    #[test]
    fn every_research_artifact_and_workspace_fault_recovers_one_atomic_prefix() {
        run_research_fault_matrix(
            &research_artifact_command("research-artifact-fault"),
            &request(
                "research.artifacts.get",
                11,
                json!({"user_id": 11, "paper_hash": "research-paper", "lang": "en"}),
                None,
            ),
            "",
        );
        run_research_fault_matrix(
            &research_workspace_command("research-workspace-fault"),
            &request(
                "research.workspace.get",
                11,
                json!({"user_id": 11, "paper_hash": "research-paper", "lang": "en"}),
                None,
            ),
            "workspace",
        );
    }

    fn seed_completed_turn(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
        turn_id: &str,
        parent_content: &str,
    ) {
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: conversation_id.to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: serde_json::to_vec(&json!({"content": parent_content})).unwrap(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("{turn_id}-seed"),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: None,
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Continuation".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
    }

    fn continuation_witness(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
        turn_id: &str,
    ) -> Value {
        let turn = execute(
            database,
            &request(
                "turn.get",
                11,
                json!({"conversation_id": conversation_id, "turn_id": turn_id, "user_id": 11}),
                None,
            ),
            1_100,
        )
        .unwrap();
        crate::model_continuation::settled_witness(turn.as_object().unwrap())
    }

    fn continuation_projection(messages: Value, turns: Vec<Value>) -> Value {
        json!({"messages": messages, "turns": turns})
    }

    fn continuation_put_request(
        conversation_id: &str,
        projection: Value,
        command_id: &str,
    ) -> Request {
        request(
            "model_continuation.put",
            11,
            json!({
                "user_id": 11,
                "conversation_id": conversation_id,
                "lane_id": "main",
                "projection": projection,
            }),
            Some(command_id),
        )
    }

    fn continuation_get_request(conversation_id: &str) -> Request {
        request(
            "model_continuation.get",
            11,
            json!({
                "user_id": 11,
                "conversation_id": conversation_id,
                "lane_id": "main",
            }),
            None,
        )
    }

    #[test]
    fn model_continuation_is_owner_scoped_frontier_cas_idempotent_and_durable() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_completed_turn(&mut database, "cont-conversation", "cont-turn", "answer");
        let witness = continuation_witness(&mut database, "cont-conversation", "cont-turn");
        let projection = continuation_projection(
            json!([{"role": "user", "content": "q"}, {"role": "assistant", "content": "answer"}]),
            vec![witness.clone()],
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_get_request("cont-conversation"),
                1_200
            )
            .unwrap(),
            Value::Null
        );
        // Malformed and foreign-witness candidates never replace state.
        let mut malformed = projection.clone();
        malformed["messages"] = json!([]);
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", malformed, "cont-put-bad"),
                1_201
            )
            .unwrap(),
            Value::Bool(false)
        );
        let foreign = continuation_projection(
            json!([{"role": "assistant", "content": "answer"}]),
            vec![json!(["foreign-turn", witness[1].clone()])],
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", foreign, "cont-put-foreign"),
                1_202
            )
            .unwrap(),
            Value::Bool(false)
        );
        // First write at frontier 1.
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", projection.clone(), "cont-put-1"),
                1_203
            )
            .unwrap(),
            Value::Bool(true)
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_get_request("cont-conversation"),
                1_204
            )
            .unwrap(),
            projection
        );
        // Equal-frontier retry with the identical payload is a read-only
        // idempotent acknowledgement; a diverging payload is rejected.
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", projection.clone(), "cont-put-2"),
                1_205
            )
            .unwrap(),
            Value::Bool(true)
        );
        let mut diverged = projection.clone();
        diverged["messages"][1]["content"] = json!("edited");
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", diverged, "cont-put-3"),
                1_206
            )
            .unwrap(),
            Value::Bool(false)
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_get_request("cont-conversation"),
                1_207
            )
            .unwrap(),
            projection
        );
        // Owner scoping: another owner sees nothing and cannot write.
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "model_continuation.get",
                    12,
                    json!({"user_id": 12, "conversation_id": "cont-conversation", "lane_id": "main"}),
                    None,
                ),
                1_208,
            )
            .unwrap(),
            Value::Null
        );
        assert_eq!(
            execute(
                &mut database,
                &request(
                    "model_continuation.get",
                    12,
                    json!({"user_id": 11, "conversation_id": "cont-conversation", "lane_id": "main"}),
                    None,
                ),
                1_209,
            ),
            Err(SemanticExecutionError::AuthorityScopeMismatch)
        );
        // A second settled Turn advances the lane ordinal; frontier 2 admits
        // the extended checkpoint, and the stale frontier-1 payload is rejected.
        seed_completed_turn(
            &mut database,
            "cont-conversation",
            "cont-turn-2",
            "answer-2",
        );
        let witness_2 = continuation_witness(&mut database, "cont-conversation", "cont-turn-2");
        let projection_2 = continuation_projection(
            json!([{"role": "assistant", "content": "answer-2"}]),
            vec![witness.clone(), witness_2],
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", projection_2.clone(), "cont-put-4"),
                1_210
            )
            .unwrap(),
            Value::Bool(true)
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_put_request("cont-conversation", projection.clone(), "cont-put-5"),
                1_211
            )
            .unwrap(),
            Value::Bool(false)
        );
        assert_eq!(
            execute(
                &mut database,
                &continuation_get_request("cont-conversation"),
                1_212
            )
            .unwrap(),
            projection_2
        );
        // Durability across reopen.
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(
            execute(
                &mut reopened,
                &continuation_get_request("cont-conversation"),
                2_000
            )
            .unwrap(),
            projection_2
        );
        // Delete reports whether a record existed.
        let delete = request(
            "model_continuation.delete",
            11,
            json!({"user_id": 11, "conversation_id": "cont-conversation", "lane_id": "main"}),
            Some("cont-delete-1"),
        );
        assert_eq!(
            execute(&mut reopened, &delete, 2_001).unwrap(),
            Value::Bool(true)
        );
        assert_eq!(
            execute(
                &mut reopened,
                &continuation_get_request("cont-conversation"),
                2_002
            )
            .unwrap(),
            Value::Null
        );
        let delete_again = request(
            "model_continuation.delete",
            11,
            json!({"user_id": 11, "conversation_id": "cont-conversation", "lane_id": "main"}),
            Some("cont-delete-2"),
        );
        assert_eq!(
            execute(&mut reopened, &delete_again, 2_003).unwrap(),
            Value::Bool(false)
        );
        // A missing conversation never manufactures a record.
        assert_eq!(
            execute(
                &mut reopened,
                &continuation_get_request("missing-conversation"),
                2_004
            )
            .unwrap(),
            Value::Null
        );
    }

    #[test]
    fn terminal_event_settles_continuation_atomically_without_manufactured_acks() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let attempt_id = "cont-settle-attempt".to_owned();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            &database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: "cont-settle-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: serde_json::to_vec(&json!({"content": "draft"})).unwrap(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "cont-settle-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "cont-settle-turn".to_owned(),
                attempt_id: Some(attempt_id.clone()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Settle".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::attempt_bind(
            &database,
            &mut bind,
            &attempt_id,
            "cont-settle-task",
            "",
            1_100,
        )
        .unwrap();
        database.commit(bind).unwrap();

        let terminal = request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id,
                "task_id": "cont-settle-task",
                "user_id": 11,
                "terminal": true,
                "status": "completed",
                "slim": true,
                "content": "final",
                "thinking": "done",
                "settlement": {"outcome": "completed", "cause": "finished", "resumeOptions": []},
                "model_continuation": {
                    "content": "final",
                    "projection": {
                        "messages": [{"role": "assistant", "content": "final"}],
                        "turns": [["cont-settle-turn", "0000000000000000000000000000000000000000000000000000000000000000"]],
                    },
                },
                "now": 1_300
            }),
            Some("cont-settle-terminal"),
        );
        let settled = execute(&mut database, &terminal, 1_300).unwrap();
        assert_eq!(settled["model_continuation_status"], "stored");
        let stored = execute(
            &mut database,
            &continuation_get_request("cont-settle-conversation"),
            1_301,
        )
        .unwrap();
        assert_eq!(
            stored["messages"],
            json!([{"role": "assistant", "content": "final"}])
        );
        let stored_witness = stored["turns"][0].as_array().unwrap();
        assert_eq!(stored_witness[0], "cont-settle-turn");
        assert_ne!(
            stored_witness[1],
            "0000000000000000000000000000000000000000000000000000000000000000"
        );
        assert_eq!(stored_witness[1].as_str().unwrap().len(), 64);
        // The settled attempt rejects a second terminal command.
        let second = request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id,
                "task_id": "cont-settle-task",
                "user_id": 11,
                "terminal": true,
                "status": "completed",
                "slim": true,
                "content": "final",
                "thinking": "done",
                "settlement": {"outcome": "completed", "cause": "finished", "resumeOptions": []},
                "now": 1_400
            }),
            Some("cont-settle-terminal-2"),
        );
        assert_eq!(
            execute(&mut database, &second, 1_400).unwrap(),
            json!({"applied": false})
        );
    }

    fn seeded_continuation_fault_database(
        vfs: std::sync::Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, Value) {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(std::path::Path::new("/data"), vfs.clone())
                .unwrap();
        seed_completed_turn(
            &mut database,
            "fault-cont-conversation",
            "fault-cont-turn",
            "durable",
        );
        let witness =
            continuation_witness(&mut database, "fault-cont-conversation", "fault-cont-turn");
        let projection = continuation_projection(
            json!([{"role": "system", "content": "x".repeat(16 * 1024)},
                   {"role": "assistant", "content": "durable"}]),
            vec![witness],
        );
        database
            .configure_logical_outbox(&[72; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        vfs.arm_fault(None).unwrap();
        (database, projection)
    }

    fn assert_continuation_fault_replay_converges(
        vfs: std::sync::Arc<DeterministicVfs>,
        projection: &Value,
    ) {
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(std::path::Path::new("/data"), vfs).unwrap();
        let stored = execute(
            &mut recovered,
            &continuation_get_request("fault-cont-conversation"),
            2_000,
        )
        .unwrap();
        assert!(
            stored == Value::Null || &stored == projection,
            "partial continuation record recovered: {stored}"
        );
        // Whether the faulted command committed or not, replaying it converges
        // to the stored checkpoint without duplicating or skipping state.
        assert_eq!(
            execute(
                &mut recovered,
                &continuation_put_request(
                    "fault-cont-conversation",
                    projection.clone(),
                    "fault-cont-put"
                ),
                2_001,
            )
            .unwrap(),
            Value::Bool(true)
        );
        assert_eq!(
            execute(
                &mut recovered,
                &continuation_get_request("fault-cont-conversation"),
                2_002,
            )
            .unwrap(),
            *projection
        );
    }

    #[test]
    fn every_model_continuation_put_commit_fault_recovers_one_atomic_prefix() {
        let baseline_vfs = prepared_vfs();
        let (mut baseline, projection) = seeded_continuation_fault_database(baseline_vfs.clone());
        assert_eq!(
            execute(
                &mut baseline,
                &continuation_put_request(
                    "fault-cont-conversation",
                    projection.clone(),
                    "fault-cont-put"
                ),
                1_500,
            )
            .unwrap(),
            Value::Bool(true)
        );
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);
        assert_continuation_fault_replay_converges(baseline_vfs, &projection);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_vfs();
            let (mut database, projection) = seeded_continuation_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &continuation_put_request(
                    "fault-cont-conversation",
                    projection.clone(),
                    "fault-cont-put",
                ),
                1_500,
            );
            drop(database);
            assert_continuation_fault_replay_converges(vfs, &projection);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_vfs();
            let (mut database, projection) = seeded_continuation_fault_database(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = execute(
                &mut database,
                &continuation_put_request(
                    "fault-cont-conversation",
                    projection.clone(),
                    "fault-cont-put",
                ),
                1_500,
            );
            drop(database);
            assert_continuation_fault_replay_converges(vfs, &projection);
        }
    }
    #[test]
    fn execution_rechecks_deadline_after_admission() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let request = request(
            "desktop.egress_agent.get",
            11,
            json!({"owner_user_id": 11}),
            None,
        );
        let admitted = admit_request(
            &request,
            NegotiatedProtocol {
                protocol_version: PROTOCOL_VERSION,
                schema_id: STORAGE_SCHEMA_VERSION,
            },
            AuthenticatedScope {
                owner_id: 11,
                tenant_id: Some(7),
            },
            request.deadline_unix_ms - 1,
        )
        .unwrap();
        assert_eq!(
            execute_admitted_request(&mut database, admitted, request.deadline_unix_ms)
                .unwrap_err(),
            SemanticExecutionError::DeadlineElapsed
        );
    }

    // ---- turn_append_large raw admission (2026-09-19): the split raw path
    // must produce byte-identical projection/settlement JSON to the legacy
    // typed normalization for canonical inputs, preserve the producer's
    // escape spelling verbatim, and fail closed on malformed shapes.

    fn legacy_projection_json(projection: &Value) -> Vec<u8> {
        let Value::Object(map) = projection.clone() else {
            panic!("fixture projection must be an object");
        };
        canonical_json(&Value::Object(normalized_turn_projection(map))).unwrap()
    }

    fn raw_projection_json(payload: &[u8]) -> Vec<u8> {
        let AppendSettledRawFields { mut raw, .. } = split_append_settled_payload(payload).unwrap();
        let projection: std::collections::BTreeMap<String, Box<serde_json::value::RawValue>> =
            serde_json::from_str(raw.remove("projection").expect("fixture carries projection").get())
                .unwrap();
        serde_json::to_vec(&normalized_turn_projection_raw(projection)).unwrap()
    }

    #[test]
    fn append_settled_raw_admission_matches_legacy_encoding_for_canonical_inputs() {
        let cases: Vec<Value> = vec![
            json!({"content": "heavily \"escaped\" \\ content 雪 and a tab\t", "provider_id": "p", "turnId": "x", "attemptId": "y"}),
            json!({"text": "plain", "role": "assistant"}),
            json!({"text": null}),
            json!({}),
            json!({"content": "", "providerId": "q", "provider_id": "drop-me"}),
            json!({"thinking": "t", "temperature": 1.5, "nested": {"a": [1, 2.5, null, {"雪": true}]}}),
            json!({"content": "x", "branches": {"drop": true}, "_turnId": "drop", "_commandPending": 1}),
        ];
        for (index, projection) in cases.iter().enumerate() {
            let payload = serde_json::to_vec(&json!({"projection": projection})).unwrap();
            let expected = legacy_projection_json(projection);
            let actual = raw_projection_json(&payload);
            assert_eq!(
                expected, actual,
                "case {index}: raw admission diverged from legacy encoding"
            );
        }
    }

    #[test]
    fn append_settled_raw_admission_preserves_the_producers_escape_spelling() {
        // Non-canonical escape spellings (A for A, \/ for /) survive the
        // raw path verbatim — the legacy typed path canonicalized them away.
        let payload = br#"{"projection":{"content":"A\/\u96ea"}}"#;
        let encoded = raw_projection_json(payload);
        let encoded_text = String::from_utf8(encoded).unwrap();
        assert!(
            encoded_text.contains("\\/"),
            "raw path must preserve the producer's escape spelling, got {encoded_text}"
        );
        let value: Value = serde_json::from_str(&encoded_text).unwrap();
        assert_eq!(value["content"], Value::String("A/雪".to_owned()));
    }

    #[test]
    fn append_settled_raw_admission_fails_closed_on_malformed_shapes() {
        // projection present but not an object.
        let payload = serde_json::to_vec(&json!({"projection": "not-an-object"})).unwrap();
        let AppendSettledRawFields { mut raw, .. } = split_append_settled_payload(&payload).unwrap();
        assert!(
            serde_json::from_str::<
                std::collections::BTreeMap<String, Box<serde_json::value::RawValue>>,
            >(raw.remove("projection").unwrap().get())
            .is_err(),
            "non-object projection must fail the raw object validation"
        );
        // malformed outer JSON.
        assert!(split_append_settled_payload(b"{\"projection\":").is_err());
        // settlement present but not an object fails its object validation too.
        let payload = serde_json::to_vec(&json!({"settlement": [1, 2, 3]})).unwrap();
        let AppendSettledRawFields { mut raw, .. } = split_append_settled_payload(&payload).unwrap();
        assert!(
            serde_json::from_str::<
                std::collections::BTreeMap<String, Box<serde_json::value::RawValue>>,
            >(raw.remove("settlement").unwrap().get())
            .is_err()
        );
    }

    #[test]
    fn append_settled_raw_canonical_request_matches_typed_digest_and_outbox() {
        // The receipt digest and outbox bytes must cover the full payload
        // and match the typed path byte for byte on canonical producers
        // (the compact form the wire client emits).
        let payloads: Vec<Value> = vec![
            json!({"conversation_id": "c", "user_id": 11, "command_id": "cmd",
                   "projection": {"content": "escaped \"quotes\" \\ and 雪", "provider_id": "p"}}),
            json!({"user_id": 1}),
            json!({"user_id": 2, "z": 1, "a": {"nested": [true, null, 2.5]}, "projection": {}}),
        ];
        for (index, payload) in payloads.iter().enumerate() {
            let bytes = serde_json::to_vec(payload).unwrap();
            let typed = canonical_request(
                "turn.append_settled",
                &payload_object(&bytes).unwrap(),
                0,
            );
            let AppendSettledRawFields { raw, .. } =
                split_append_settled_payload(&bytes).unwrap();
            let rawd = canonical_request_raw("turn.append_settled", &raw, 0);
            assert_eq!(rawd.digest, typed.digest, "case {index}: digest diverged");
            assert_eq!(rawd.bytes, typed.bytes, "case {index}: canonical bytes diverged");
            assert_eq!(
                rawd.ownerless_digest, typed.ownerless_digest,
                "case {index}: ownerless digest diverged"
            );
        }
    }

    /// Regression test for the production integrity fence `CONTROL payload
    /// manifest references a missing segment`: an out-of-band-deleted
    /// segment holding a blob-carried record value must fail the affected
    /// operation with `SemanticExecutionError::Integrity`, mutate nothing,
    /// and stay fenced across a restart.
    ///
    /// Deterministic by construction: the blob chunk is pre-packed into the
    /// FIRST published segment while no entity exists at all, so nothing
    /// else (entity pages, receipts, outbox state) can reference that
    /// segment. The record write then dedupes its blob chunk onto the
    /// packed block instead of restaging it, and a later checkpoint places
    /// every entity page in a second segment — deleting the first fences
    /// the blob read but not open/replay, exactly the production shape.
    #[test]
    fn missing_payload_segment_fences_the_operation_with_integrity_and_no_mutation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[72; 32], 4 * 1024 * 1024, 2 * 1024 * 1024)
            .unwrap();
        // A value above the inline envelope threshold is blob-carried. Its
        // single chunk block is committed and checkpointed alone, so the
        // first segment holds exactly that block and nothing else.
        let value = json!("x".repeat(100_000));
        let chunk = crate::blob::encode_chunk(&serde_json::to_vec(&value).unwrap()).unwrap();
        database
            .commit_block_payloads_for_test(&[chunk])
            .unwrap();
        database.checkpoint_for_test().unwrap();
        let segments_dir = directory.path().join("payload-segments");
        let first_generation: Vec<std::path::PathBuf> = std::fs::read_dir(&segments_dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "pseg"))
            .collect();
        assert_eq!(
            first_generation.len(),
            1,
            "the lone chunk block must pack into exactly one segment"
        );
        // The record's blob chunk dedupes onto the packed block; the second
        // checkpoint lands every entity page in a new segment.
        let put_blob = request(
            "record.put",
            11,
            json!({"namespace": "fenced", "key": "doc", "value": value}),
            Some("fenced-put-doc"),
        );
        execute(&mut database, &put_blob, 1_000).unwrap();
        database.checkpoint_for_test().unwrap();
        let after_second: Vec<std::path::PathBuf> = std::fs::read_dir(&segments_dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.extension().is_some_and(|ext| ext == "pseg"))
            .collect();
        assert_eq!(after_second.len(), 2, "expected one segment per checkpoint");
        let durable_before = database
            .writer_engine()
            .unwrap()
            .state()
            .durable_sequence;
        drop(database);
        // Out-of-band loss of the segment holding the blob-carried value.
        std::fs::remove_file(&first_generation[0]).unwrap();
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let get_blob = request(
            "record.get",
            11,
            json!({"namespace": "fenced", "key": "doc"}),
            None,
        );
        match execute(&mut database, &get_blob, 2_000).unwrap_err() {
            SemanticExecutionError::Integrity(message) => assert!(
                message.contains("missing segment"),
                "unexpected integrity diagnostic: {message}"
            ),
            other => panic!("expected an integrity fence, got {other:?}"),
        }
        // The fenced operation mutated nothing, and the fence persists
        // across a restart — the production symptom.
        let durable_after = database
            .writer_engine()
            .unwrap()
            .state()
            .durable_sequence;
        assert_eq!(durable_after, durable_before);
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        match execute(&mut database, &get_blob, 2_100).unwrap_err() {
            SemanticExecutionError::Integrity(message) => assert!(
                message.contains("missing segment"),
                "unexpected integrity diagnostic after reopen: {message}"
            ),
            other => panic!("expected a persistent integrity fence, got {other:?}"),
        }
        assert_eq!(
            database
                .writer_engine()
                .unwrap()
                .state()
                .durable_sequence,
            durable_before
        );
    }

    #[test]
    fn integrity_failure_bounds_its_diagnostic_to_the_wire_budget() {
        let long = "x".repeat(5000);
        match integrity_failure(&long) {
            SemanticExecutionError::Integrity(message) => {
                assert!(message.len() <= 403, "len={}", message.len());
                assert!(message.ends_with("..."));
            }
            other => panic!("unexpected variant: {other:?}"),
        }
        match integrity_failure("stored turn projection\nis missing segments") {
            SemanticExecutionError::Integrity(message) => {
                assert_eq!(message, "stored turn projection is missing segments");
            }
            other => panic!("unexpected variant: {other:?}"),
        }
        // Multibyte input truncates on a char boundary, never mid-UTF-8.
        match integrity_failure(&"界".repeat(200)) {
            SemanticExecutionError::Integrity(message) => {
                assert!(message.len() <= 403, "len={}", message.len());
                assert!(std::str::from_utf8(message.as_bytes()).is_ok());
            }
            other => panic!("unexpected variant: {other:?}"),
        }
    }

    #[test]
    fn background_receipt_pages_bound_bytes_without_losing_results() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(&mut database, &request("conversation.create", 11,
            json!({"conv_id":"receipt-pages","user_id":11}), Some("pages-create")), 1000).unwrap();
        let text = "汉\\\"".repeat(30_000);
        for index in 0..32 {
            execute(&mut database, &request("queue.enqueue", 11,
                json!({"conv_id":"receipt-pages","user_id":11,"queue_id":format!("page-{index}"),
                    "kind":"background_receipt","priority":80,"created_at_ms":1001,
                    "message":{"text":text,"_backgroundCommand":format!("bg_{index}")},"config":{}}),
                Some(&format!("pages-enqueue-{index}"))), 1001).unwrap();
        }
        let mut cursor = Value::Null;
        let mut seen = std::collections::BTreeSet::new();
        let mut pages = 0;
        loop {
            let page = execute(&mut database, &request("queue.list", 11,
                json!({"conv_id":"receipt-pages","user_id":11,"mode":"background_page",
                    "cursor":cursor,"max_bytes":1_000_000}), None), 1002).unwrap();
            assert!(serde_json::to_vec(&page).unwrap().len() <= 1_000_000);
            assert!(!page["items"].as_array().unwrap().is_empty());
            for item in page["items"].as_array().unwrap() {
                assert_eq!(item["payload"]["text"], text);
                assert!(seen.insert(item["queueId"].as_str().unwrap().to_owned()));
            }
            pages += 1;
            let next = page["nextCursor"].clone();
            if next.is_null() { break; }
            assert_ne!(next, cursor);
            cursor = next;
            assert!(pages <= 32);
        }
        assert!(pages > 1);
        assert_eq!(seen.len(), 32);
        assert!(execute(&mut database, &request("queue.list", 11,
            json!({"conv_id":"receipt-pages","user_id":11,"mode":"background_page", "max_bytes":1024}), None), 1003).is_err());
    }

    #[test]
    fn event_list_materialized_page_overflow_is_payload_size_not_availability() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let text = "x".repeat(5 * 1024 * 1024);
        for sequence in 0..2 {
            execute(&mut database, &request("event.append", 11, json!({
                "task_id": "large-inspector-page", "sequence": sequence,
                "event": {"type": "messages_snapshot", "text": text},
                "created_at_ms": 1000
            }), Some(&format!("page-seed-{sequence}"))), 1000).unwrap();
        }
        let read = |after, limit| request("event.list", 11, json!({
            "task_id": "large-inspector-page", "after_sequence": after,
            "limit": limit, "types": ["messages_snapshot"]
        }), None);
        let error = execute(&mut database, &read(-1, 1000), 1000).unwrap_err();
        assert_eq!(error.code(), "storage_payload_too_large");
        assert!(!error.retryable());
        for after in [-1, 0] {
            let rows = execute(&mut database, &read(after, 1), 1000).unwrap();
            assert_eq!(rows.as_array().unwrap().len(), 1);
            assert_eq!(rows[0]["sequence"], after + 1);
            assert_eq!(rows[0]["event"]["text"].as_str().unwrap().len(), text.len());
        }
        assert_eq!(execute(&mut database, &read(1, 1), 1000).unwrap(), json!([]));
    }

#[test]
fn timer_counts_and_recovery_feed_have_no_live_or_saved_row_admission_quota() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.configure_timer_live_capacity(1).unwrap();
    let started = std::time::Instant::now();
    for index in 0..650 {
        execute(&mut database, &timer_create_request(
            11, &format!("timer-demand-{index:04}"), "conv-demand", "2026-09-01",
            &format!("timer-demand-create-{index:04}")), 1000+index).unwrap();
    }
    let all = execute(&mut database, &request("timer.active.list_all", 11, json!({"limit":0}), None), 2000).unwrap();
    assert_eq!(all.as_array().unwrap().len(), 650);
    assert_eq!(execute(&mut database, &request("timer.active.count", 11, json!({"user_id":11}), None), 2001).unwrap(), json!(650));
    let projected = execute(&mut database, &request("timer.active.list_all", 11, json!({"limit":20}), None), 2002).unwrap();
    assert_eq!(projected.as_array().unwrap().len(), 20);
    eprintln!("timer demand: 650 durable active rows plus complete recovery feed, {:?}", started.elapsed());
    assert_eq!(database.snapshot_pin_metrics().unwrap().retained_transaction_bytes, 0);
}
