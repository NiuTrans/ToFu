// A compact admitted patch is not a new full-document literal. Both ordinary
// growth and stable-segment repair can cross 8 MiB inside the authority.
#[test]
fn large_projection_small_patch_and_terminal_settlement_remain_writable() {
    for (stable_segments, append_bytes) in [
        (false, 30_000), (true, 10_000), (true, 9 * 1024 * 1024),
    ] {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut db,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "large-patch", "user_id": 11, "title": "Test", "settings": {}
                }),
                Some("large-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut db,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "large-patch", "user_id": 11, "command_id": "seed",
                    "actor": "assistant", "kind": "reply", "status": "completed",
                    "projection": {}, "settlement": {}, "turn_id": "turn"
                }),
                Some("large-seed"),
            ),
            1_100,
        )
        .unwrap();
        let config = if append_bytes > 8 * 1024 * 1024 {
            json!({"retainedContext": "c".repeat(9 * 1024 * 1024)})
        } else { json!({}) };
        let attempt = execute(
            &mut db,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "large-patch", "turn_id": "turn", "user_id": 11,
                    "command_id": "go", "operation": "regenerate", "target_actor": "assistant",
                    "target_kind": "reply", "expected_projection_revision": 1, "config": config,
                    "dispatch_mode": "conversation_executor"
                }),
                Some("large-attempt"),
            ),
            1_200,
        )
        .unwrap();
        let attempt_id = attempt["attempt"]["attemptId"].as_str().unwrap();
        let dispatchable = execute(&mut db, &request("turn.attempt.dispatchable.list", 99,
            json!({"created_before_ms": 1_250, "limit": 32}), None), 1_250).unwrap();
        assert!(dispatchable[0]["config"]["retainedContext"] == config["retainedContext"],
            "dispatch recovery must retain the complete supplied context");
        assert!(dispatchable[0]["config"]["_resumeSource"].is_object());
        assert_eq!(dispatchable[0]["attempt"]["attemptId"], attempt_id);
        execute(
            &mut db,
            &request(
                "turn.attempt.bind",
                11,
                json!({
                    "attempt_id": attempt_id, "task_id": "task", "user_id": 11
                }),
                Some("large-bind"),
            ),
            1_300,
        )
        .unwrap();
        let live = execute(
            &mut db,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id, "task_id": "task", "user_id": 11,
                    "status": "running", "event_type": "phase", "event_payload": {},
                    "projection": {"content": "x".repeat(8_370_000), "segments": []}
                }),
                Some("large-full"),
            ),
            1_400,
        )
        .unwrap();
        let revision = live["projection_revision"].as_u64().unwrap();
        let patch_request = request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id, "task_id": "task", "user_id": 11,
                "status": "running", "event_type": "phase", "event_payload": {},
                "projection_segments_stable": stable_segments,
                "projection_patch": {"version": 1, "baseRevision": revision,
                    "targetRevision": revision + 1, "operations": [
                        {"op": "append_text", "path": ["content"], "value": "y".repeat(append_bytes)}
                    ]}
            }),
            Some("large-patch-event"),
        );
        assert!(patch_request.payload.len() < append_bytes + 2048);
        let patched = execute(&mut db, &patch_request, 1_500).unwrap();
        assert_eq!(patched["applied"], true);
        let get = request(
            "turn.get",
            11,
            json!({
                "conversation_id": "large-patch", "turn_id": "turn", "user_id": 11
            }),
            None,
        );
        // Reload the newly folded >8 MiB checkpoint, not merely the compact Turn.
        let live_turn = execute(&mut db, &get, 1_510).unwrap();
        assert_eq!(
            live_turn["projection"]["content"].as_str().unwrap().len(),
            8_370_000 + append_bytes
        );
        assert_eq!(live_turn["status"], "running");
        assert!(
            serde_json::to_vec(&live_turn["projection"]).unwrap().len()
                > crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES
        );
        let terminal_request = request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id, "task_id": "task", "user_id": 11,
                "status": "failed", "terminal": true,
                "projection_patch": {"version": 1, "baseRevision": revision + 1,
                    "targetRevision": revision + 2, "operations": []},
                "event_type": "terminal_settlement", "event_payload": {},
                "settlement": {"outcome": "failed", "cause": "synthetic"},
                "error": {"message": "synthetic"}
            }),
            Some("large-terminal"),
        );
        let terminal = execute(&mut db, &terminal_request, 1_600).unwrap();
        assert_eq!(terminal["applied"], true);
        // A lost terminal ACK replays its original receipt without another
        // event publication, revision bump or settlement.
        assert_eq!(
            execute(&mut db, &terminal_request, 1_700).unwrap(),
            terminal
        );
        // Settlement retires the head, retains all text, and survives reopen.
        drop(db);
        let mut db = AuthorityDatabase::open(directory.path()).unwrap();
        let settled = execute(&mut db, &get, 1_800).unwrap();
        assert_eq!(settled["status"], "failed");
        assert_eq!(settled["settlement"]["cause"], "synthetic");
        assert_eq!(
            settled["projection"]["content"],
            live_turn["projection"]["content"]
        );
        assert_eq!(
            settled["projection"]["segments"],
            live_turn["projection"]["segments"]
        );
        assert_eq!(settled["projectionRevision"], revision + 2);
        let foreign = execute(
            &mut db,
            &request(
                "turn.get",
                12,
                json!({
                    "conversation_id": "large-patch", "turn_id": "turn", "user_id": 12
                }),
                None,
            ),
            1_900,
        );
        assert!(matches!(
            foreign,
            Ok(Value::Null) | Err(SemanticExecutionError::NotFound)
        ));
    }
}

// Compact heads are physical compression, never permission to admit an
// un-settleable materialized revision. Exercise the real ceiling, not a mock.
#[test]
fn visible_and_related_turn_publication_preserves_content_above_eight_mib() {
    let directory = tempfile::tempdir().unwrap();
    let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
    let pair = execute(&mut db, &request("turn.create_pair", 11, json!({
        "conversation_id": "large-related", "user_id": 11, "command_id": "create",
        "input_projection": {"content": "hello"}, "config": {},
        "conversation_defaults": {"allowCreate": true, "title": "Related"}
    }), Some("large-related-create")), 1_000).unwrap();
    let attempt = pair["attempt"]["attemptId"].as_str().unwrap();
    let root = pair["turn"]["turnId"].as_str().unwrap();
    execute(&mut db, &request("turn.attempt.bind", 11, json!({
        "attempt_id": attempt, "task_id": "large-related-task", "user_id": 11
    }), Some("large-related-bind")), 1_100).unwrap();
    let content = "v".repeat(9 * 1024 * 1024);
    execute(&mut db, &request("turn.visible.sync", 11, json!({
        "conversation_id": "large-related", "attempt_id": attempt, "root_turn_id": root,
        "default_kind": "flow_node", "run_id": "large-visible-run", "user_id": 11,
        "messages": [
            {"role": "assistant", "_isFlowPlanner": true, "content": "plan"},
            {"role": "user", "_isFlowReview": true, "_flowApproved": false, "content": content}
        ]
    }), Some("large-related-visible")), 1_200).unwrap();
    let turns = execute(&mut db, &request("turn.list", 11, json!({
        "conversation_id": "large-related", "user_id": 11
    }), None), 1_300).unwrap();
    let related = turns.as_array().unwrap().iter()
        .find(|turn| turn["projection"]["content"] == content).unwrap();
    let related_id = related["turnId"].as_str().unwrap();
    execute(&mut db, &request("turn.related.announce", 11, json!({
        "attempt_id": attempt, "turn_ids": [related_id], "user_id": 11
    }), Some("large-related-announce")), 1_400).unwrap();
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    let replay = execute(&mut db, &request("turn.sync.changes", 11, json!({
        "conversation_id": "large-related", "user_id": 11, "after": 0, "limit": 100
    }), None), 1_500).unwrap();
    let last = replay["events"].as_array().unwrap().last().unwrap();
    assert_eq!(last["type"], "attempt.event");
    assert!(last["payload"]["event"]["payload"]["turns"][0]["projection"]["content"]
        .as_str() == Some(content.as_str()), "related replay must preserve the complete content");
}

#[test]
fn compact_head_ceiling_rejects_atomically_and_retains_settleable_state() {
    for recover in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut db,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "ceiling", "user_id": 11, "title": "Test", "settings": {}
                }),
                Some("ceiling-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut db,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "ceiling", "user_id": 11, "command_id": "seed",
                    "actor": "assistant", "kind": "reply", "status": "completed",
                    "projection": {}, "settlement": {}, "turn_id": "turn"
                }),
                Some("ceiling-seed"),
            ),
            1_100,
        )
        .unwrap();
        let attempt = execute(
            &mut db,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "ceiling", "turn_id": "turn", "user_id": 11,
                    "command_id": "go", "operation": "regenerate", "target_actor": "assistant",
                    "target_kind": "reply", "expected_projection_revision": 1,
                    "config": {"model": "test-model"}
                }),
                Some("ceiling-attempt"),
            ),
            1_200,
        )
        .unwrap();
        let attempt_id = attempt["attempt"]["attemptId"].as_str().unwrap();
        execute(
            &mut db,
            &request(
                "turn.attempt.bind",
                11,
                json!({
                    "attempt_id": attempt_id, "task_id": "task", "user_id": 11
                }),
                Some("ceiling-bind"),
            ),
            1_300,
        )
        .unwrap();
        let live = execute(
            &mut db,
            &request(
                "turn.event.record",
                11,
                json!({
                    "attempt_id": attempt_id, "task_id": "task", "user_id": 11,
                    "status": "running", "event_type": "phase", "event_payload": {},
                    "projection": {"content": "x".repeat(8_000_000), "segments": []}
                }),
                Some("ceiling-full"),
            ),
            1_400,
        )
        .unwrap();
        let mut revision = live["projection_revision"].as_u64().unwrap();
        let append = |owner: u64, revision: u64, bytes: usize| {
            request(
                "turn.event.record",
                owner,
                json!({
                    "attempt_id": attempt_id, "task_id": "task", "user_id": owner,
                    "status": "running", "event_type": "phase", "event_payload": {},
                    "projection_patch": {"version": 1, "baseRevision": revision,
                        "targetRevision": revision + 1, "operations": [
                            {"op": "append_text", "path": ["content"], "value": "y".repeat(bytes)}
                        ]}
                }),
                Some(&format!("ceiling-append-{owner}-{revision}-{bytes}")),
            )
        };
        let mut content_bytes = 8_000_000;
        while content_bytes < 58_400_000 {
            let added = (58_400_000 - content_bytes).min(8_000_000);
            assert_eq!(
                execute(&mut db, &append(11, revision, added), 1_500 + revision).unwrap()
                    ["applied"],
                true
            );
            content_bytes += added;
            revision += 1;
        }
        // This small append leaves an unfurled checkpoint-plus-patch chain.
        assert_eq!(
            execute(&mut db, &append(11, revision, 100_000), 1_600).unwrap()["applied"],
            true
        );
        content_bytes += 100_000;
        revision += 1;
        assert_eq!(
            execute(&mut db, &append(12, revision, 1), 1_610).unwrap(),
            json!({"applied": false})
        );
        // Both the materialized budget and incoming admission remain enforced.
        assert!(execute(&mut db, &append(11, revision, 100_000), 1_620).is_err());
        assert!(execute(&mut db, &append(11, revision, 8_388_608), 1_630).is_err());
        drop(db);
        let mut db = AuthorityDatabase::open(directory.path()).unwrap();
        let get = request(
            "turn.get",
            11,
            json!({
                "conversation_id": "ceiling", "turn_id": "turn", "user_id": 11
            }),
            None,
        );
        let live = execute(&mut db, &get, 1_700).unwrap();
        assert_eq!(live["projectionRevision"], revision);
        assert_eq!(
            live["projection"]["content"].as_str().unwrap().len(),
            content_bytes
        );
        assert_eq!(live["status"], "running");
        drop(live);
        let terminal = request(
            "turn.event.record",
            11,
            json!({
                "attempt_id": attempt_id, "task_id": "task", "user_id": 11,
                "status": "failed", "terminal": true, "event_type": "terminal_settlement",
                "event_payload": {}, "settlement": {"cause": "synthetic", "detail": "m".repeat(100_000)},
                "projection_patch": {"version": 1, "baseRevision": revision,
                    "targetRevision": revision + 1, "operations": []}
            }),
            Some("ceiling-terminal"),
        );
        let expected_retry = if recover {
            let result = execute(
                &mut db,
                &request(
                    "turn.recover",
                    11,
                    json!({"user_id": 11}),
                    Some("ceiling-recover"),
                ),
                1_800,
            )
            .unwrap();
            assert_eq!(result["recovered"], 1);
            json!({"applied": false})
        } else {
            let result = execute(&mut db, &terminal, 1_800).unwrap();
            assert_eq!(result["applied"], true);
            result
        };
        drop(db);
        let mut db = AuthorityDatabase::open(directory.path()).unwrap();
        let settled = execute(&mut db, &get, 1_900).unwrap();
        assert_eq!(
            settled["status"],
            if recover { "interrupted" } else { "failed" }
        );
        assert_eq!(settled["projectionRevision"], revision + 1);
        assert_eq!(
            settled["projection"]["content"].as_str().unwrap().len(),
            content_bytes
        );
        assert_eq!(
            execute(&mut db, &terminal, 2_000).unwrap(),
            expected_retry
        );
    }
}

#[test]
fn continued_tool_round_patch_repairs_replays_and_settles() {
        let directory = tempfile::tempdir().unwrap();
        let mut db = AuthorityDatabase::initialize(directory.path()).unwrap();
        execute(
            &mut db,
            &request(
                "conversation.create",
                11,
                json!({
                    "conv_id": "continued-tools", "user_id": 11, "title": "Test", "settings": {}
                }),
                Some("continued-create"),
            ),
            1_000,
        )
        .unwrap();
        execute(
            &mut db,
            &request(
                "turn.append_settled",
                11,
                json!({
                    "conversation_id": "continued-tools", "user_id": 11, "command_id": "seed",
                    "actor": "assistant", "kind": "reply", "status": "completed",
                    "projection": {}, "settlement": {}, "turn_id": "turn"
                }),
                Some("continued-seed"),
            ),
            1_100,
        )
        .unwrap();
        let attempt = execute(
            &mut db,
            &request(
                "turn.attempt.create",
                11,
                json!({
                    "conversation_id": "continued-tools", "turn_id": "turn", "user_id": 11,
                    "command_id": "go", "operation": "regenerate", "target_actor": "assistant",
                    "target_kind": "reply", "expected_projection_revision": 1, "config": {}
                }),
                Some("continued-attempt"),
            ),
            1_200,
        )
        .unwrap();
        let attempt_id = attempt["attempt"]["attemptId"].as_str().unwrap();
        execute(
            &mut db,
            &request(
                "turn.attempt.bind",
                11,
                json!({
                    "attempt_id": attempt_id, "task_id": "task", "user_id": 11
                }),
                Some("continued-bind"),
            ),
            1_300,
        )
        .unwrap();

    let rounds = json!([{"toolCallId":"old-call", "toolName":"read", "toolArgs":"{}", "toolContent":"durable result", "status":"completed", "attemptId":"old"}]);
    let live = execute(&mut db, &request("turn.event.record", 11, json!({
        "attempt_id":attempt_id, "task_id":"task", "user_id":11,
        "status":"running", "event_type":"phase", "event_payload":{},
        "projection":{"content":"prefix", "thinking":"", "segments":[], "toolRounds":rounds}
    }), Some("continued-shell")), 1_400).unwrap();
    let revision = live["projection_revision"].as_u64().unwrap();
    let terminal = request("turn.event.record", 11, json!({
        "attempt_id":attempt_id, "task_id":"task", "user_id":11,
        "status":"failed", "terminal":true, "projection_segments_stable":true,
        "projection_patch":{"version":1, "baseRevision":revision, "targetRevision":revision+1,
            "operations":[{"op":"set", "path":["segments",0,"text"], "value":"failure recorded"}]},
        "event_type":"terminal_settlement", "event_payload":{},
        "settlement":{"outcome":"failed", "cause":"generation_error"},
        "error":{"message":"synthetic"}
    }), Some("continued-terminal"));
    let result = execute(&mut db, &terminal, 1_500).unwrap();
    assert_eq!(result["applied"], true);
    assert_eq!(execute(&mut db, &terminal, 1_600).unwrap(), result);
    let replay = execute(&mut db, &request("turn.events.list",11,json!({
        "attempt_id":attempt_id, "user_id":11, "projection_mode":"patch"
    }),None),1_610).unwrap();
    let event = replay.as_array().unwrap().last().unwrap();
    let raw_base = json!({"content":"prefix", "thinking":"", "segments":[], "toolRounds":rounds});
    let replayed = crate::turn_projection_patch::apply_projection_patch(
        raw_base.as_object(), event["payload"]["projectionPatch"].as_object().unwrap()
    ).unwrap();
    let before_reopen = execute(&mut db, &request("turn.get",11,json!({
        "conversation_id":"continued-tools", "turn_id":"turn", "user_id":11
    }),None),1_620).unwrap();
    assert_eq!(Value::Object(replayed), before_reopen["projection"]);
    drop(db);
    let mut db = AuthorityDatabase::open(directory.path()).unwrap();
    let settled = execute(&mut db, &request("turn.get",11,json!({
        "conversation_id":"continued-tools", "turn_id":"turn", "user_id":11
    }),None),1_700).unwrap();
    assert_eq!(settled["status"],"failed");
    assert_eq!(settled["projectionRevision"],revision+1);
    assert_eq!(settled["projection"]["toolRounds"],rounds);
    assert_eq!(settled["projection"]["segments"][0]["text"],"failure recorded");
    assert_eq!(settled["projection"]["segments"][1]["id"],"old-call");
    assert_eq!(settled["projection"]["segments"][1]["result"]["content"],"durable result");
}
