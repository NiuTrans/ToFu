    use super::*;

    fn fixture_turn(content: &str) -> Value {
        json!({
            "turnId": "turn-1", "conversationId": "conv-1", "laneId": "main",
            "ordinal": 3, "actor": "human", "kind": "pair", "status": "completed",
            "projection": {"content": content},
            "settlement": {"outcome": "completed"},
            "createdAt": 1_800_000_000_000_u64, "updatedAt": 1_800_000_000_001_u64,
        })
    }

    fn raw_of(value: &Value) -> Box<RawValue> {
        RawValue::from_string(serde_json::to_string(value).unwrap()).unwrap()
    }

    #[test]
    fn raw_document_splice_matches_canonical_serialization_byte_for_byte() {
        let hostile = format!("{}\"\\\n\u{1f}é{}",
            "z".repeat(200_000), "q".repeat(50_000));
        let projection = json!({
            "content": hostile,
            "timestamp": 1_800_000_000_000_u64,
            "nested": {"a": [1, 2, 3], "b": null},
            "unicode": "汉字",
        });
        let projection_bytes = serde_json::to_vec(&projection).unwrap();
        let document = Map::from_iter([
            ("_first".to_owned(), json!(1)),
            ("actor".to_owned(), json!("human")),
            ("nestedArr".to_owned(), json!([{"k": "v"}, 2.5, null])),
            ("ordinal".to_owned(), json!(7)),
            ("projection".to_owned(), projection),
            ("status".to_owned(), json!("completed")),
            ("zLast".to_owned(), json!(true)),
        ]);
        let expected = serde_json::to_vec(&Value::Object(document.clone())).unwrap();
        let actual =
            serialize_document_with_raw_projection(&document, &projection_bytes).unwrap();
        assert_eq!(actual, expected);
    }

    #[test]
    fn raw_turn_upsert_encoding_matches_the_value_path_byte_for_byte() {
        let big = "x".repeat(300_000);
        let turn = fixture_turn(&big);
        let turn_raw = raw_of(&turn);
        let attempt = json!({"attemptId": "a-1", "status": "completed"});
        for (queue, removed, turn_id, attempt_id) in [
            (vec![], vec![], None, None),
            (vec![json!({"queueId": "q-1", "position": 1})], vec![],
             Some("turn-1"), Some("a-1")),
            (vec![], vec!["q-9".to_string()], Some("turn-1"), None),
            (vec![json!({"queueId": "q-2"})], vec!["q-8".to_string()],
             None, Some("a-1")),
        ] {
            let revision = 41_u64;
            let sequence = 9_u64;
            let occurred_at_ms = 1_800_000_000_042_u64;
            // Value path (previous construction).
            let mut payload = json!({
                "turns": [turn.clone()],
                "attempts": [attempt.clone()],
                "conversationRevision": revision
            });
            if !queue.is_empty() {
                payload["queueItemUpserts"] = Value::Array(queue.clone());
            }
            if !removed.is_empty() {
                payload["removedQueueIds"] = serde_json::to_value(&removed).unwrap();
            }
            let event = conversation_sync_event(
                "conv-1", sequence, "turn.upsert", occurred_at_ms, payload,
                turn_id, attempt_id,
            );
            let expected = serde_json::to_vec(&event).unwrap();
            // Raw path (current construction).
            let raw_event = TurnUpsertSyncEvent {
                attempt_id: attempt_id.filter(|identity| !identity.is_empty()),
                contract: "tofu.conversation-sync.event/v1",
                conversation_id: "conv-1",
                occurred_at_ms,
                payload: TurnUpsertPayload {
                    attempts: vec![&attempt],
                    conversation_revision: revision,
                    queue_item_upserts: queue.clone(),
                    removed_queue_ids: removed.clone(),
                    turns: vec![&turn_raw],
                },
                sync_sequence: sequence,
                turn_id: turn_id.filter(|identity| !identity.is_empty()),
                change_type: "turn.upsert",
            };
            let actual = serde_json::to_vec(&raw_event).unwrap();
            assert_eq!(actual, expected, "queue={queue:?} removed={removed:?}");
        }
    }

    #[test]
    fn settled_sync_event_splice_matches_the_derive_encoding_byte_for_byte() {
        let hostile = format!("\"\\\n\u{1f}é汉字{}{}", "z".repeat(120_000), "q".repeat(700));
        let turn = fixture_turn(&hostile);
        let turn_bytes = serde_json::to_vec(&turn).unwrap();
        let turn_raw = raw_of(&turn);
        let attempt = json!({
            "attemptId": "a-1\"esc", "status": "completed",
            "nested": {"unicode": "汉字", "arr": [1, null, 2.5]}
        });
        let revision = 41_u64;
        let sequence = 9_u64;
        let occurred_at_ms = 1_800_000_000_042_u64;
        for (turn_id, attempt_id, attempt) in [
            (None, None, None),
            (Some("turn-1\"\\\n"), Some("a-1é"), Some(&attempt)),
            (Some("turn-1"), None, None),
            (None, Some("a-1"), Some(&attempt)),
            (Some(""), Some(""), Some(&attempt)),
        ] {
            // Derive path (the generic `turn.upsert` encoding).
            let derive_event = TurnUpsertSyncEvent {
                attempt_id: attempt_id.filter(|identity| !identity.is_empty()),
                contract: "tofu.conversation-sync.event/v1",
                conversation_id: "conv-1\"\\\n\u{1f}",
                occurred_at_ms,
                payload: TurnUpsertPayload {
                    attempts: attempt.iter().map(|value| *value).collect(),
                    conversation_revision: revision,
                    queue_item_upserts: Vec::new(),
                    removed_queue_ids: Vec::new(),
                    turns: vec![&turn_raw],
                },
                sync_sequence: sequence,
                turn_id: turn_id.filter(|identity| !identity.is_empty()),
                change_type: "turn.upsert",
            };
            let expected = serde_json::to_vec(&derive_event).unwrap();
            let actual = settled_turn_upsert_event_bytes(
                "conv-1\"\\\n\u{1f}",
                turn_id,
                attempt_id,
                revision,
                occurred_at_ms,
                sequence,
                &turn_bytes,
                attempt,
            )
            .unwrap();
            assert_eq!(
                actual, expected,
                "turn_id={turn_id:?} attempt_id={attempt_id:?}"
            );
        }
    }

    #[test]
    fn settled_response_splice_matches_the_derive_encoding_byte_for_byte() {
        let hostile = format!("\"\\\n\u{1f}é{}{}", "y".repeat(90_000), "w".repeat(300));
        let turn = fixture_turn(&hostile);
        let turn_bytes = serde_json::to_vec(&turn).unwrap();
        let turn_raw = raw_of(&turn);
        let attempt = json!({"attemptId": "a-1\"esc", "status": "completed"});
        for attempt_ref in [None, Some(attempt.clone())] {
            let expected = encode_response_raw(&AppendSettledResponse {
                attempt: &attempt_ref,
                conversation_revision: 7,
                turn: &turn_raw,
            })
            .unwrap();
            let actual =
                append_settled_response_bytes(&attempt_ref, 7, &turn_bytes).unwrap();
            assert_eq!(actual, expected, "attempt={attempt_ref:?}");
        }
    }

    #[test]
    fn raw_response_encodings_match_the_value_path_byte_for_byte() {
        let big = "y".repeat(280_000);
        let turn = fixture_turn(&big);
        let submitted = fixture_turn("submitted");
        let turn_raw = raw_of(&turn);
        let submitted_raw = raw_of(&submitted);
        let attempt = json!({"attemptId": "a-1", "status": "completed"});
        // append_settled response.
        let expected = serde_json::to_vec(&json!({
            "turn": turn, "attempt": Value::Null, "conversationRevision": 7
        }))
        .unwrap();
        let actual = encode_response_raw(&AppendSettledResponse {
            attempt: &None,
            conversation_revision: 7,
            turn: &turn_raw,
        })
        .unwrap();
        assert_eq!(actual, expected);
        // create_pair response, with and without queue fields.
        let base = json!({
            "submittedTurn": submitted, "turn": turn, "attempt": attempt,
            "conversationRevision": 9, "streamCursor": 1, "idempotentReplay": false,
            "_needsStart": false
        });
        for (queue_id, queue_item) in [
            (None, None),
            (Some("q-1"), Some(json!({"queueId": "q-1", "position": 2}))),
        ] {
            let mut expected = base.clone();
            if let Some(item) = &queue_item {
                expected["queued"] = Value::Bool(true);
                expected["queueId"] = Value::String(queue_id.unwrap().to_owned());
                expected["position"] = item.get("position").cloned().unwrap_or(Value::Null);
                expected["queueItem"] = item.clone();
            }
            let response = CreatePairResponse {
                needs_start: false,
                attempt: &attempt,
                conversation_revision: 9,
                idempotent_replay: false,
                position: queue_item
                    .as_ref()
                    .map(|item| item.get("position").cloned().unwrap_or(Value::Null)),
                queue_id: queue_id.map(str::to_owned),
                queue_item: queue_item.clone(),
                queued: queue_item.is_some(),
                stream_cursor: 1,
                submitted_turn: &submitted_raw,
                turn: &turn_raw,
            };
            assert_eq!(
                encode_response_raw(&response).unwrap(),
                serde_json::to_vec(&expected).unwrap(),
            );
        }
        // queue-resume response.
        let expected = serde_json::to_vec(&json!({
            "submittedTurn": submitted, "turn": turn, "attempt": attempt,
            "conversationRevision": 5, "streamCursor": 1, "idempotentReplay": false,
            "queued": false, "queueId": "q-7", "_needsStart": true
        }))
        .unwrap();
        let actual = encode_response_raw(&QueueResumeResponse {
            needs_start: true,
            attempt: &attempt,
            conversation_revision: 5,
            idempotent_replay: false,
            queue_id: "q-7",
            queued: false,
            stream_cursor: 1,
            submitted_turn: &submitted_raw,
            turn: &turn_raw,
        })
        .unwrap();
        assert_eq!(actual, expected);
    }

    #[test]
    fn turn_upsert_age_locator_matches_the_event_value_path() {
        let event = json!({
            "contract": "tofu.conversation-sync.event/v1", "type": "turn.upsert",
            "conversationId": "conv-1", "syncSeq": 9, "occurredAt": 1_800_000_000_042_u64,
            "payload": {}
        });
        let expected = sync_age_index_value(&event).unwrap();
        let locator = json!({
            "conversationId": "conv-1",
            "occurredAt": 1_800_000_000_042_u64,
            "syncSeq": 9,
        });
        assert_eq!(serde_json::to_vec(&locator).unwrap(), expected);
    }

    #[test]
    fn search_page_budget_publishes_partial_at_turn_archive_boundary_without_starving_work() {
        use crate::turn_search_projection::{
            ConversationSearchRequest, TurnSearchDocument, TurnSearchProjection,
        };
        use crate::turn_search_worker::process_turn_search_batch;
        use std::sync::{Arc, Mutex};

        let source_dir = tempfile::tempdir().unwrap();
        let target_dir = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(source_dir.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut transaction,
            &crate::conversation_header::CreateRequest {
                conversation_id: "conversation".into(),
                title: "source header".into(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 100,
                updated_at_ms: 100,
                committed_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let authority = Arc::new(Mutex::new(database));
        let projection = Arc::new(Mutex::new(
            TurnSearchProjection::initialize(target_dir.path(), 16 * 1024 * 1024).unwrap(),
        ));
        let scope = crate::maintenance::MaintenanceScope::new(7, 11).unwrap();
        process_turn_search_batch(&authority, &projection, scope).unwrap();
        {
            let mut target = projection.lock().unwrap();
            target
                .begin_conversation_rebuild(7, 11, "conversation", "previous", 100)
                .unwrap();
            target
                .append_conversation_page(
                    7,
                    11,
                    "conversation",
                    "previous",
                    &[TurnSearchDocument {
                        turn_id: "previous-turn".into(),
                        ordinal: 1,
                        role: "user".into(),
                        match_field: "content".into(),
                        search_text: "previous searchable content".into(),
                        chunk_index: 0,
                    }],
                )
                .unwrap();
            target
                .finalize_conversation_rebuild(7, 11, "conversation", "previous")
                .unwrap();
        }
        let search = |query: &str| {
            projection
                .lock()
                .unwrap()
                .search(&ConversationSearchRequest {
                    tenant_id: 7,
                    owner_user_id: 11,
                    query: query.into(),
                    limit: 50,
                    snippet_radius: 10,
                })
                .unwrap()
        };
        let maximum_pages = crate::generated_tofudb_ir::SEARCH_PROJECTION_MAXIMUM_SOURCE_PAGES;
        let maximum_turns = maximum_pages * SEARCH_PROJECTION_PAGE_ROWS;
        // Populate only the canonical primary records used by this source scan.
        // Small inline documents and 256-row commits avoid an 8,192-command
        // semantic/receipt fixture while exercising the actual 1,024-page bound.
        let seed = |database: &mut AuthorityDatabase, first: usize, end: usize| {
            for start in (first..end).step_by(256) {
                let mut transaction = database.begin(7, 11).unwrap();
                for index in start..(start + 256).min(end) {
                    let turn_id = format!("turn-{index:05}");
                    let key = turn_key(&transaction, "conversation", &turn_id).unwrap();
                    crate::versioned_document::put(database, &mut transaction,
                        crate::versioned_document::PutRequest {
                            key,
                            namespace: DOCUMENT_IDENTITY.into(),
                            logical_key: turn_logical_key("conversation", &turn_id),
                            value_json: serde_json::to_vec(&json!({
                                "turnId": turn_id, "conversationId": "conversation", "laneId": "main",
                                "actor": "human", "status": if index == 0 { "completed" } else { "running" },
                                "ordinal": index + 1, "projection": {"content": "replacement searchable content"}
                            })).unwrap(),
                            expected_version: None, updated_at_ms: 200,
                        }).unwrap();
                }
                crate::search_dirty::mark(
                    database,
                    &mut transaction,
                    TURN_SEARCH_DIRTY_NAMESPACE,
                    "conversation",
                )
                .unwrap();
                database.commit(transaction).unwrap();
            }
        };
        seed(&mut authority.lock().unwrap(), 0, maximum_turns);
        for extra in [false, true] {
            if extra {
                seed(
                    &mut authority.lock().unwrap(),
                    maximum_turns,
                    maximum_turns + 1,
                );
            }
            let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
            assert_eq!(metrics.source_pages, maximum_pages as u64);
            assert_eq!(metrics.rebuilt_conversations, 1);
            assert_eq!(metrics.page_budget_exhaustions, 1);
            assert!(projection.lock().unwrap().source_incomplete(7, 11).unwrap());
            assert!(search("previous searchable").is_empty());
            assert_eq!(search("replacement searchable").len(), 1);
            assert_eq!(
                authority
                    .lock()
                    .unwrap()
                    .snapshot_pin_metrics()
                    .unwrap()
                    .active_handles,
                0
            );
            assert!(crate::search_dirty::list(&authority.lock().unwrap(), 7, 11)
                .unwrap()
                .is_empty());
            let retry = process_turn_search_batch(&authority, &projection, scope).unwrap();
            assert_eq!(
                retry.source_pages, 0,
                "unchanged partial sources must not retry forever"
            );
            assert_eq!(retry.rebuilt_conversations, 0);
        }
        // A later dirty conversation must progress beyond the oversized source.
        {
            let mut database = authority.lock().unwrap();
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::conversation_header::create(
                &database,
                &mut transaction,
                &crate::conversation_header::CreateRequest {
                    conversation_id: "later-conversation".into(),
                    title: "later fixture".into(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                    updated_at_ms: 100,
                    committed_at_ms: 100,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        let later = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(later.rebuilt_conversations, 1);
        assert_eq!(later.page_budget_exhaustions, 0);
        assert_eq!(search("later fixture").len(), 1);
        // Reducing the source by one page leaves room for the archive page.
        // The next exact-limit rebuild must clear partial coverage.
        {
            let mut database = authority.lock().unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            for index in maximum_turns - SEARCH_PROJECTION_PAGE_ROWS..=maximum_turns {
                let key =
                    turn_key(&transaction, "conversation", &format!("turn-{index:05}")).unwrap();
                database.entity_delete(&mut transaction, key).unwrap();
            }
            crate::search_dirty::mark(
                &database,
                &mut transaction,
                TURN_SEARCH_DIRTY_NAMESPACE,
                "conversation",
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }
        let metrics = process_turn_search_batch(&authority, &projection, scope).unwrap();
        assert_eq!(metrics.source_pages, maximum_pages as u64);
        assert_eq!(metrics.rebuilt_conversations, 1);
        assert_eq!(metrics.page_budget_exhaustions, 0);
        assert!(!projection.lock().unwrap().source_incomplete(7, 11).unwrap());
        assert_eq!(search("replacement searchable").len(), 1);
        assert!(search("previous searchable").is_empty());
        let database = authority.lock().unwrap();
        assert_eq!(database.snapshot_pin_metrics().unwrap().active_handles, 0);
        assert!(crate::search_dirty::list(&database, 7, 11)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn restart_settlement_preserves_prefill_checkpoint_and_human_guidance_options() {
        let attempt = json!({"_config": {"model": "gpt-4o"}})
            .as_object()
            .unwrap()
            .clone();
        let projection = json!({
            "content": "partial 🚀",
            "thinking": "working",
            "toolRounds": [
                {
                    "toolCallId": "call-1", "toolName": "search",
                    "toolArgs": "not-json", "toolContent": "result"
                },
                {
                    "_providerAttemptDiscarded": true,
                    "toolCallId": "discarded", "toolName": "search"
                },
                {
                    "toolCallId": "ask-1", "toolName": "ask_human",
                    "status": "awaiting_human", "guidanceId": "guide-1",
                    "guidanceQuestion": "Continue?", "guidanceType": "confirm"
                }
            ]
        })
        .as_object()
        .unwrap()
        .clone();
        let settlement = restart_settlement(&attempt, &projection);
        assert_eq!(settlement["cause"], "server_restart");
        assert_eq!(settlement["resumeOptions"][0]["operation"], "continue");
        assert_eq!(settlement["resumeOptions"][0]["anchor"]["contentChars"], 9);
        assert_eq!(
            settlement["resumeOptions"][1]["anchor"],
            json!({
                "type": "tool_checkpoint",
                "keptToolRounds": 2,
                "replayableToolRounds": 1,
                "retainedToolRoundPositions": [0],
                "content": "", "thinking": "", "segments": []
            })
        );
        assert_eq!(
            settlement["resumeOptions"][2]["operation"],
            "answer_guidance"
        );
        assert_eq!(
            settlement["resumeOptions"][2]["anchor"]["retainedToolRoundPositions"],
            json!([0])
        );
        assert_eq!(settlement["resumeOptions"][3]["operation"], "regenerate");

        let claude = json!({"_config": {"model": "aws.anthropic.claude-opus-5"}})
            .as_object()
            .unwrap()
            .clone();
        let prose_only = json!({"content": "partial"}).as_object().unwrap().clone();
        assert_eq!(
            restart_settlement(&claude, &prose_only)["resumeOptions"],
            json!([{"operation": "regenerate", "anchor": {"type": "turn_start"}}])
        );
    }

    #[test]
    fn continuation_settlement_validates_candidates_without_false_storage_ack() {
        let projection = json!({"content": "final"}).as_object().unwrap().clone();
        let check = |status: &str, candidate: Value, expected: &str| {
            assert_eq!(
                terminal_continuation_candidate(status, &candidate, "turn", &projection).err(),
                Some(expected)
            );
        };
        check("completed", Value::Null, "unavailable");
        check("failed", json!({"projection": {}}), "not_completed");
        check("cancelled", Value::Null, "not_completed");
        check("completed", json!({"status": "over_budget"}), "over_budget");
        check("completed", json!({"status": "stored"}), "unavailable");
        check("completed", json!({"status": "x".repeat(65)}), "invalid");
        check(
            "completed",
            json!({"projection": {}}),
            "invalid_or_over_budget",
        );
        let candidate = json!({"content": "final", "projection": {
            "messages": [{"role": "assistant"}], "turns": [["turn", "a".repeat(64)]]
        }});
        assert!(
            terminal_continuation_candidate("completed", &candidate, "turn", &projection).is_ok()
        );
        let mut changed = candidate.clone();
        changed["content"] = json!("stale");
        check("completed", changed, "authority_changed");
        let mut foreign = candidate.clone();
        foreign["projection"]["turns"][0][0] = json!("foreign-turn");
        check("completed", foreign, "authority_changed");
        let mut malformed = candidate.clone();
        malformed["projection"]["turns"][0][1] = json!("A".repeat(64));
        check("completed", malformed, "invalid_or_over_budget");
        let mut multibyte = candidate.clone();
        multibyte["projection"]["padding"] = json!("中".repeat(3 * 1024 * 1024));
        check("completed", multibyte, "invalid_or_over_budget");
        let mut too_many_messages = candidate.clone();
        too_many_messages["projection"]["messages"] =
            json!(vec![json!({"role": "assistant"}); 16_385]);
        check("completed", too_many_messages, "invalid_or_over_budget");
        let mut too_many_witnesses = candidate.clone();
        too_many_witnesses["projection"]["turns"] =
            json!(vec![json!(["turn", "a".repeat(64)]); 4_097]);
        check("completed", too_many_witnesses, "invalid_or_over_budget");
        let mut oversized = candidate;
        oversized["projection"]["padding"] = json!("x".repeat(8 * 1024 * 1024));
        check("completed", oversized, "invalid_or_over_budget");
    }

    fn live_projection_request(attempt_id: &str, content: &str, now_ms: u64) -> EventRecordRequest {
        EventRecordRequest {
            model_continuation: Value::Null,
            attempt_id: attempt_id.to_owned(),
            task_id: "projection-head-task".to_owned(),
            terminal: false,
            status: "running".to_owned(),
            slim: true,
            content: content.to_owned(),
            thinking: "working".to_owned(),
            projection: Value::Null,
            projection_patch: None,
            projection_segments_stable: false,
            settlement: Map::new(),
            error: Map::new(),
            event_type: "projection_updated".to_owned(),
            event_payload: Map::new(),
            background_command_ack: None,
            task_event: None, preceding_task_events: Vec::new(),
            flow_root_attempt_id: String::new(),
            now_ms,
        }
    }

    #[test]
    fn large_live_projection_uses_bounded_head_and_terminal_materializes_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let projection = json!({
            "content": "",
            "thinking": "",
            "largeStableEvidence": "x".repeat(128 * 1024),
        });
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "projection-head-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "projection-head-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "projection-head-turn".to_owned(),
                attempt_id: Some("projection-head-attempt".to_owned()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Projection head".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();

        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "projection-head-attempt",
            "projection-head-task",
            "",
            1_100,
        )
        .unwrap();
        database.commit(bind).unwrap();

        let mut live = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut live,
            &live_projection_request("projection-head-attempt", "first", 1_200),
        )
        .unwrap();
        database.commit(live).unwrap();

        let head = {
            let mut inspect = database.begin(7, 11).unwrap();
            let physical_turn_key = turn_key(
                &inspect,
                "projection-head-conversation",
                "projection-head-turn",
            )
            .unwrap();
            let stored = database
                .entity_get(&mut inspect, &physical_turn_key)
                .unwrap()
                .unwrap();
            let (_, physical_json) = crate::versioned_document::materialize_stored_document(
                &database,
                7,
                11,
                &stored,
                DOCUMENT_IDENTITY,
            )
            .unwrap();
            assert!(physical_json.len() < 8 * 1024);
            let physical = decode_turn_value(&physical_json).unwrap();
            assert_eq!(physical["projection"], json!({}));
            let head = projection_head_from_document(&physical).unwrap().unwrap();
            assert_eq!(head.patch_count, 1);
            let logical: Value = serde_json::from_slice(
                &get(
                    &database,
                    &mut inspect,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap()
                .unwrap(),
            )
            .unwrap();
            assert_eq!(logical["projection"]["content"], "first");
            assert_eq!(
                logical["projection"]["largeStableEvidence"]
                    .as_str()
                    .unwrap()
                    .len(),
                128 * 1024
            );
            let mut settled_with_head = physical.clone();
            settled_with_head.insert("status".to_owned(), Value::String("completed".to_owned()));
            assert_eq!(
                projection_head_from_document(&settled_with_head)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::InvalidData
            );
            let mut wrong_revision = physical;
            wrong_revision.insert("projectionRevision".to_owned(), Value::from(999));
            assert_eq!(
                projection_head_from_document(&wrong_revision)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::InvalidData
            );
            head
        };

        {
            let mut missing_checkpoint = database.begin(7, 11).unwrap();
            let key = projection_checkpoint_key(
                &missing_checkpoint,
                "projection-head-conversation",
                "projection-head-turn",
                &head.head_id,
            )
            .unwrap();
            database
                .entity_delete(&mut missing_checkpoint, key)
                .unwrap();
            assert_eq!(
                get(
                    &database,
                    &mut missing_checkpoint,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap_err()
                .kind(),
                io::ErrorKind::InvalidData
            );
        }
        {
            let mut missing_patch = database.begin(7, 11).unwrap();
            let key = projection_patch_key(
                &missing_patch,
                "projection-head-conversation",
                "projection-head-turn",
                &head.head_id,
                head.base_revision + 1,
            )
            .unwrap();
            database.entity_delete(&mut missing_patch, key).unwrap();
            assert_eq!(
                get(
                    &database,
                    &mut missing_patch,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap_err()
                .kind(),
                io::ErrorKind::InvalidData
            );
        }

        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut second = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut second,
            &live_projection_request("projection-head-attempt", "second", 1_300),
        )
        .unwrap();
        database.commit(second).unwrap();

        for index in 2..MAX_TURN_PROJECTION_HEAD_PATCHES {
            let mut frame = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            event_record(
                &database,
                &mut frame,
                &live_projection_request(
                    "projection-head-attempt",
                    &format!("frame-{index}"),
                    1_300 + index as u64,
                ),
            )
            .unwrap();
            database.commit(frame).unwrap();
        }
        {
            let mut inspect = database.begin(7, 11).unwrap();
            let key = turn_key(
                &inspect,
                "projection-head-conversation",
                "projection-head-turn",
            )
            .unwrap();
            let stored = database.entity_get(&mut inspect, &key).unwrap().unwrap();
            let (_, physical_json) = crate::versioned_document::materialize_stored_document(
                &database,
                7,
                11,
                &stored,
                DOCUMENT_IDENTITY,
            )
            .unwrap();
            let current =
                projection_head_from_document(&decode_turn_value(&physical_json).unwrap())
                    .unwrap()
                    .unwrap();
            assert_eq!(current.head_id, head.head_id);
            assert_eq!(current.patch_count, MAX_TURN_PROJECTION_HEAD_PATCHES);
        }
        let mut rollover = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut rollover,
            &live_projection_request("projection-head-attempt", "rollover", 2_000),
        )
        .unwrap();
        database.commit(rollover).unwrap();
        {
            let mut inspect = database.begin(7, 11).unwrap();
            let key = turn_key(
                &inspect,
                "projection-head-conversation",
                "projection-head-turn",
            )
            .unwrap();
            let stored = database.entity_get(&mut inspect, &key).unwrap().unwrap();
            let (_, physical_json) = crate::versioned_document::materialize_stored_document(
                &database,
                7,
                11,
                &stored,
                DOCUMENT_IDENTITY,
            )
            .unwrap();
            let current =
                projection_head_from_document(&decode_turn_value(&physical_json).unwrap())
                    .unwrap()
                    .unwrap();
            assert_ne!(current.head_id, head.head_id);
            assert_eq!(current.patch_count, 0);
            let logical: Value = serde_json::from_slice(
                &get(
                    &database,
                    &mut inspect,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap()
                .unwrap(),
            )
            .unwrap();
            assert_eq!(logical["projection"]["content"], "rollover");
        }
        let mut terminal_request =
            live_projection_request("projection-head-attempt", "final", 2_100);
        terminal_request.terminal = true;
        terminal_request.status = "completed".to_owned();
        terminal_request.thinking = "done".to_owned();
        terminal_request.settlement = json!({
            "outcome": "completed",
            "cause": "finished",
            "resumeOptions": [],
        })
        .as_object()
        .unwrap()
        .clone();
        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let result: Value = serde_json::from_slice(
            &event_record(&database, &mut terminal, &terminal_request).unwrap(),
        )
        .unwrap();
        assert_eq!(result["model_continuation_status"], "unavailable");
        database.commit(terminal).unwrap();

        let mut verify = database.begin(7, 11).unwrap();
        let physical_turn_key = turn_key(
            &verify,
            "projection-head-conversation",
            "projection-head-turn",
        )
        .unwrap();
        let stored = database
            .entity_get(&mut verify, &physical_turn_key)
            .unwrap()
            .unwrap();
        let (_, physical_json) = crate::versioned_document::materialize_stored_document(
            &database,
            7,
            11,
            &stored,
            DOCUMENT_IDENTITY,
        )
        .unwrap();
        let physical = decode_turn_value(&physical_json).unwrap();
        assert!(projection_head_from_document(&physical).unwrap().is_none());
        assert_eq!(physical["projection"]["content"], "final");
        let (head_start, head_end) = projection_head_range(
            &verify,
            "projection-head-conversation",
            "projection-head-turn",
            &head.head_id,
        )
        .unwrap();
        assert!(database
            .entity_scan(&mut verify, &head_start, &head_end, 1)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn conversation_lifecycle_restores_and_purges_projection_heads() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let projection = json!({
            "content": "",
            "thinking": "",
            "largeStableEvidence": "x".repeat(128 * 1024),
        });
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "projection-head-lifecycle-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "projection-head-lifecycle-turn".to_owned(),
                attempt_id: Some("projection-head-lifecycle-attempt".to_owned()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Projection head lifecycle".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "projection-head-lifecycle-attempt",
            "projection-head-task",
            "",
            1_100,
        )
        .unwrap();
        database.commit(bind).unwrap();
        let mut live = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut live,
            &live_projection_request("projection-head-lifecycle-attempt", "live", 1_200),
        )
        .unwrap();
        database.commit(live).unwrap();

        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &mut database,
            &mut delete,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                deleted_at_ms: 1_300,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();
        let mut restore = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::restore(
            &mut database,
            &mut restore,
            &crate::conversation_header::RestoreRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                committed_at_ms: 1_400,
            },
        )
        .unwrap();
        database.commit(restore).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let restored: Value = serde_json::from_slice(
            &get(
                &database,
                &mut read,
                "projection-head-lifecycle",
                "projection-head-lifecycle-turn",
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(restored["status"], "interrupted");
        assert_eq!(restored["projection"]["content"], "live");
        assert_eq!(
            restored["projection"]["largeStableEvidence"]
                .as_str()
                .unwrap()
                .len(),
            128 * 1024
        );
        drop(read);

        let mut purge = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::purge(
            &mut database,
            &mut purge,
            &crate::conversation_header::PurgeRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                purged_at_ms: 1_500,
            },
        )
        .unwrap();
        database.commit(purge).unwrap();
        let mut verify = database.begin(7, 11).unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            TURN_PROJECTION_HEAD_NAMESPACE,
            &conversation_prefix("projection-head-lifecycle").unwrap(),
        )
        .unwrap();
        assert!(database
            .entity_scan(&mut verify, &start, &end, 1)
            .unwrap()
            .is_empty());
    }

    fn perception_observation(index: usize) -> Map<String, Value> {
        json!({
            "observationId": format!("paint:{index}"),
            "attemptId": "attempt-a",
            "kind": "phase_painted",
            "clientId": "page-a",
            "phase": "waiting_model",
            "detailKey": "status.waitingModel",
            "serverEmittedAt": 1_000 + index,
            "receivedAt": 1_125 + index,
            "paintedAt": 1_160 + index,
        })
        .as_object()
        .unwrap()
        .clone()
    }

    #[test]
    fn perception_observations_are_closed_idempotent_and_row_bounded() {
        let mut trace = Map::new();
        for index in 0..69 {
            let (next, applied) = append_perception_observation(
                &trace,
                &perception_observation(index),
                "task-a",
                "attempt-a",
                2_000 + index as u64,
            )
            .unwrap();
            assert!(applied);
            trace = next;
        }
        let observations = trace["clientObservations"].as_array().unwrap();
        assert_eq!(observations.len(), MAX_TIMING_TRACE_CLIENT_OBSERVATIONS);
        assert_eq!(trace["clientObservationDroppedCount"], 5);
        assert_eq!(observations.last().unwrap()["renderMs"], 35);
        assert_eq!(observations.last().unwrap()["transportMs"], 125);
        let (replayed, applied) = append_perception_observation(
            &trace,
            &perception_observation(68),
            "task-a",
            "attempt-a",
            9_999,
        )
        .unwrap();
        assert!(!applied);
        assert_eq!(replayed, trace);

        let mut forbidden = perception_observation(70);
        forbidden.insert("content".to_owned(), Value::String("secret".to_owned()));
        assert!(!perception_observation_is_valid(&forbidden, "attempt-a"));
    }

    #[test]
    fn timing_trace_compaction_enforces_the_durable_byte_ceiling() {
        let noisy_rows = (0..TRACE_MAX_SPANS)
            .map(|index| {
                json!({
                    "spanId": format!("span-{index}"),
                    "name": "x".repeat(400),
                    "attrs": (0..32).map(|child| {
                        (format!("field-{child}"), Value::String("y".repeat(400)))
                    }).collect::<Map<_, _>>()
                })
            })
            .collect::<Vec<_>>();
        let document = json!({
            "version": 1,
            "taskId": "task-budget",
            "summary": {"totalMs": 1, "overBudget": noisy_rows.clone()},
            "spans": noisy_rows,
            "statusHistory": (0..TRACE_MAX_STATUS_ENTRIES).map(|index| {
                json!({"id": format!("status-{index}"), "detail": "z".repeat(400)})
            }).collect::<Vec<_>>(),
            "clientObservations": (0..MAX_TIMING_TRACE_CLIENT_OBSERVATIONS).map(|index| {
                json!({"observationId": format!("paint-{index}"), "reason": "r".repeat(160)})
            }).collect::<Vec<_>>(),
        });
        let compacted = compact_timing_trace(document.as_object().unwrap()).unwrap();
        assert!(serde_json::to_vec(&compacted).unwrap().len() <= MAX_TIMING_TRACE_PERSISTED_BYTES);
        assert_eq!(compacted["compacted"], true);
        assert!(compacted["droppedSpans"].as_u64().unwrap_or(0) > 0);
    }

    fn append_live_attempt(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        turn_id: &str,
        attempt_id: &str,
    ) {
        append_settled(
            database,
            transaction,
            &AppendSettledRequest {
                conversation_id: conversation_id.to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: br#"{"content":"","thinking":"","segments":[],"toolRounds":[]}"#
                    .to_vec(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("command-{attempt_id}"),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: Some(attempt_id.to_owned()),
                created_at_ms: 1,
                committed_at_ms: 1,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Claim fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1,
                },
            },
        )
        .unwrap();
    }

    fn append_historical_attempt(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        turn_id: &str,
        current_attempt_id: &str,
        attempt_id: &str,
        created_at_ms: u64,
    ) {
        let attempt = Map::from_iter([
            ("attemptId".to_owned(), Value::String(attempt_id.to_owned())),
            (
                "conversationId".to_owned(),
                Value::String(conversation_id.to_owned()),
            ),
            ("turnId".to_owned(), Value::String(turn_id.to_owned())),
            (
                "commandId".to_owned(),
                Value::String(format!("command-{attempt_id}")),
            ),
            (
                "taskId".to_owned(),
                Value::String(format!("{attempt_id}-task")),
            ),
            (
                "operation".to_owned(),
                Value::String("regenerate".to_owned()),
            ),
            ("status".to_owned(), Value::String("completed".to_owned())),
            ("baseProjectionRevision".to_owned(), Value::from(1)),
            ("resumeAnchor".to_owned(), Value::Object(Map::new())),
            ("createdAt".to_owned(), Value::from(created_at_ms)),
            ("startedAt".to_owned(), Value::from(created_at_ms + 1)),
            ("settledAt".to_owned(), Value::from(created_at_ms + 2)),
            (
                "_dispatchMode".to_owned(),
                Value::String("conversation_executor".to_owned()),
            ),
        ]);
        let attempt_key = attempt_key(transaction, conversation_id, attempt_id).unwrap();
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: attempt_key,
                namespace: "generation_attempts".to_owned(),
                logical_key: attempt_id.to_owned(),
                value_json: serde_json::to_vec(&Value::Object(attempt.clone())).unwrap(),
                expected_version: Some(0),
                updated_at_ms: created_at_ms + 2,
            },
        )
        .unwrap();
        let claim_key =
            global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id).unwrap();
        database
            .entity_put(
                transaction,
                claim_key,
                encode_attempt_locator(transaction.owner_user_id(), conversation_id).unwrap(),
            )
            .unwrap();
        let previous_count = decode_u64(
            database
                .entity_get(
                    transaction,
                    &attempt_turn_count_key(transaction, conversation_id, turn_id).unwrap(),
                )
                .unwrap(),
            "turn attempt count is malformed",
        )
        .unwrap();
        append_attempt_turn_directory(
            database,
            transaction,
            conversation_id,
            turn_id,
            Some(current_attempt_id),
            &attempt,
        )
        .unwrap();
        let count_key = attempt_turn_count_key(transaction, conversation_id, turn_id).unwrap();
        database
            .entity_put(
                transaction,
                count_key,
                (previous_count + 1).to_le_bytes().to_vec(),
            )
            .unwrap();
        put_attempt_timing_indexes(database, transaction, &attempt).unwrap();
    }

    #[test]
    fn attempt_event_prune_is_owner_scoped_bounded_and_preserves_the_turn() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "retention-conversation",
            "retention-turn",
            "retention-attempt",
        );
        database.commit(seed).unwrap();

        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "retention-attempt",
            "projection-head-task",
            "",
            2,
        )
        .unwrap();
        database.commit(bind).unwrap();
        let mut live = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut live,
            &live_projection_request("retention-attempt", "partial", 3),
        )
        .unwrap();
        database.commit(live).unwrap();

        let request = EventPruneRequest {
            settled_before_ms: 10,
            max_attempts: 16,
            max_rows: 10,
        };
        let mut live_prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_prune(&database, &mut live_prune, &request).unwrap()
            )
            .unwrap(),
            json!({"deleted_rows":0,"deleted_attempts":0,"remaining":0})
        );
        drop(live_prune);

        let mut terminal_request = live_projection_request("retention-attempt", "final", 4);
        terminal_request.terminal = true;
        terminal_request.status = "completed".to_owned();
        terminal_request.settlement = json!({
            "outcome": "completed", "cause": "provider_finished", "resumeOptions": []
        })
        .as_object()
        .unwrap()
        .clone();
        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(&database, &mut terminal, &terminal_request).unwrap();
        database.commit(terminal).unwrap();

        let mut foreign = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_prune(&database, &mut foreign, &request).unwrap()
            )
            .unwrap(),
            json!({"deleted_rows":0,"deleted_attempts":0,"remaining":0})
        );
        drop(foreign);

        let mut first_sync_prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &sync_prune(
                    &database,
                    &mut first_sync_prune,
                    &SyncPruneRequest {
                        created_before_ms: 10,
                        max_rows: 1,
                    },
                )
                .unwrap()
            )
            .unwrap(),
            json!({"deletedRows":1,"remaining":true})
        );
        database.commit(first_sync_prune).unwrap();
        let mut protected = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_prune(&database, &mut protected, &request).unwrap()
            )
            .unwrap(),
            json!({"deleted_rows":0,"deleted_attempts":0,"remaining":1})
        );
        drop(protected);
        let mut finish_sync_prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &sync_prune(
                    &database,
                    &mut finish_sync_prune,
                    &SyncPruneRequest {
                        created_before_ms: 10,
                        max_rows: 10,
                    },
                )
                .unwrap()
            )
            .unwrap(),
            json!({"deletedRows":3,"remaining":false})
        );
        database.commit(finish_sync_prune).unwrap();

        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let first_result: Value = serde_json::from_slice(
            &events_prune(
                &database,
                &mut first,
                &EventPruneRequest {
                    max_rows: 1,
                    ..request.clone()
                },
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            first_result,
            json!({"deleted_rows":1,"deleted_attempts":0,"remaining":1})
        );
        database.commit(first).unwrap();

        let mut remaining = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let retained_events: Value = serde_json::from_slice(
            &events_list(&database, &mut remaining, "retention-attempt", 0, 10, true, &mut || false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(retained_events.as_array().unwrap().len(), 2);
        drop(remaining);

        let mut finish = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let finish_result: Value =
            serde_json::from_slice(&events_prune(&database, &mut finish, &request).unwrap())
                .unwrap();
        assert_eq!(
            finish_result,
            json!({"deleted_rows":2,"deleted_attempts":1,"remaining":0})
        );
        database.commit(finish).unwrap();

        let mut verify = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_list(&database, &mut verify, "retention-attempt", 0, 10, true, &mut || false)
                    .unwrap()
                    .unwrap()
            )
            .unwrap(),
            json!([])
        );
        let turn: Value = serde_json::from_slice(
            &get(
                &database,
                &mut verify,
                "retention-conversation",
                "retention-turn",
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(turn["projection"]["content"], "final");
    }

    #[test]
    fn sync_prune_retires_attempt_references_after_conversation_delete() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "deleted-retention-conversation",
            "deleted-retention-turn",
            "deleted-retention-attempt",
        );
        database.commit(seed).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "deleted-retention-attempt",
            "deleted-retention-task",
            "",
            2,
        )
        .unwrap();
        database.commit(bind).unwrap();
        let mut terminal_request = live_projection_request("deleted-retention-attempt", "final", 3);
        terminal_request.terminal = true;
        terminal_request.status = "completed".to_owned();
        terminal_request.settlement = json!({
            "outcome": "completed", "cause": "provider_finished", "resumeOptions": []
        })
        .as_object()
        .unwrap()
        .clone();
        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(&database, &mut terminal, &terminal_request).unwrap();
        database.commit(terminal).unwrap();

        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &mut database,
            &mut delete,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "deleted-retention-conversation".to_owned(),
                deleted_at_ms: 4,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();

        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &sync_prune(
                    &database,
                    &mut prune,
                    &SyncPruneRequest {
                        created_before_ms: 10,
                        max_rows: 10,
                    },
                )
                .unwrap()
            )
            .unwrap(),
            json!({"deletedRows":0,"remaining":false})
        );
        database.commit(prune).unwrap();

        let mut verify = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let prefix = attempt_sync_reference_prefix("deleted-retention-attempt").unwrap();
        let (start, end) =
            EntityKey::prefix_range(7, 11, ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE, &prefix)
                .unwrap();
        assert!(database
            .entity_scan(&mut verify, &start, &end, 1)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn sync_changes_returns_resumable_prefix_when_the_deadline_elapses() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (turn, attempt) in [
            ("deadline-turn-1", "deadline-attempt-1"),
            ("deadline-turn-2", "deadline-attempt-2"),
            ("deadline-turn-3", "deadline-attempt-3"),
        ] {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(&database, &mut seed, "deadline-conversation", turn, attempt);
            database.commit(seed).unwrap();
        }
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            sync_head(&database, &mut read, "deadline-conversation").unwrap(),
            3
        );
        // Two materialization checks pass, the third reports the deadline
        // spent: the page must truncate to the delivered prefix and stay
        // resumable instead of holding the authority mutex to completion.
        let mut checks = 0;
        let page: Value = serde_json::from_slice(
            &sync_changes(&database, &mut read, "deadline-conversation", 0, 500, &mut || {
                checks += 1;
                checks > 2
            })
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(page["resetRequired"], json!(false));
        assert_eq!(page["hasMore"], json!(true));
        let events = page["events"].as_array().unwrap();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["syncSeq"], json!(1));
        assert_eq!(events[1]["syncSeq"], json!(2));
        drop(read);
        // The caller resumes from the last delivered sequence and drains the
        // remainder; nothing about the truncated page corrupts the replay.
        let mut resume = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let rest: Value = serde_json::from_slice(
            &sync_changes(&database, &mut resume, "deadline-conversation", 2, 500, &mut || false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(rest["hasMore"], json!(false));
        let events = rest["events"].as_array().unwrap();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0]["syncSeq"], json!(3));
    }

    #[test]
    fn sync_changes_without_budget_is_deadline_elapsed_not_an_empty_page() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "deadline-conversation",
            "deadline-turn",
            "deadline-attempt",
        );
        database.commit(seed).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        // An empty page would tell the caller it is caught up; with zero
        // events delivered the only honest answer is `deadline_elapsed`.
        let error = sync_changes(
            &database,
            &mut read,
            "deadline-conversation",
            0,
            500,
            &mut || true,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn sync_changes_returns_resumable_prefix_when_the_page_budget_elapses() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        // One ~62 KiB projection per event: the 500-event replay page crosses
        // its 8 MiB wire budget after ~130 of these — exactly the 2026-09-28
        // wedge shape, where a swarm-fat conversation hard-failed every
        // lagging SSE replay with `database_resource_exhausted` forever.
        const TOTAL_EVENTS: u64 = 140;
        let fat_projection = json!({
            "content": "x".repeat(62_000),
            "thinking": "",
            "segments": [],
            "toolRounds": []
        })
        .to_string()
        .into_bytes();
        for index in 0..TOTAL_EVENTS {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_settled(
                &database,
                &mut seed,
                &AppendSettledRequest {
                    conversation_id: "budget-conversation".to_owned(),
                    actor: "assistant".to_owned(),
                    status: "pending".to_owned(),
                    projection_json: fat_projection.clone(),
                    projection_timestamp: None,
                    settlement_json: b"{}".to_vec(),
                    lane_id: "main".to_owned(),
                    command_id: format!("command-budget-{index}"),
                    kind: "reply".to_owned(),
                    run_id: String::new(),
                    turn_id: format!("budget-turn-{index}"),
                    attempt_id: Some(format!("budget-attempt-{index}")),
                    created_at_ms: 1,
                    committed_at_ms: 1,
                    defaults: TurnDefaults {
                        allow_create: true,
                        title: "Budget fixture".to_owned(),
                        settings_json: b"{}".to_vec(),
                        created_at_ms: 1,
                    },
                },
            )
            .unwrap();
            database.commit(seed).unwrap();
        }
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            sync_head(&database, &mut read, "budget-conversation").unwrap(),
            TOTAL_EVENTS
        );
        // The budget-truncated page must NOT fail (the pre-fix wedge): each
        // call delivers a contiguous, resumable prefix with honest `hasMore`.
        let mut delivered: Vec<u64> = Vec::new();
        let mut pages = 0_u32;
        let mut after = 0_u64;
        loop {
            pages += 1;
            assert!(pages <= 8, "truncated replay stopped making progress");
            let page_bytes = sync_changes(
                &database,
                &mut read,
                "budget-conversation",
                after,
                500,
                &mut || false,
            )
            .unwrap()
            .unwrap();
            assert!(
                page_bytes.len() <= MAX_TRANSACTION_IR_LITERAL_BYTES + 256,
                "truncated page still exceeds the wire budget"
            );
            let page: Value = serde_json::from_slice(&page_bytes).unwrap();
            assert_eq!(page["resetRequired"], json!(false));
            let events = page["events"].as_array().unwrap();
            assert!(!events.is_empty(), "a progressable page was answered empty");
            for event in events {
                delivered.push(event["syncSeq"].as_u64().unwrap());
            }
            after = *delivered.last().unwrap();
            if page["hasMore"] == json!(false) {
                break;
            }
        }
        // Truncation actually engaged (one page cannot hold 140 × 62 KiB),
        // and replay safety holds: no frame is dropped, duplicated, or
        // reordered across the truncated pages.
        assert!(pages >= 2, "the fat fixture never crossed the page budget");
        let expected: Vec<u64> = (1..=TOTAL_EVENTS).collect();
        assert_eq!(delivered, expected);
    }

    #[test]
    fn events_list_returns_partial_prefix_when_the_deadline_elapses() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "deadline-conversation",
            "deadline-turn",
            "deadline-attempt",
        );
        database.commit(seed).unwrap();
        let mut claim = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_claim(&database, &mut claim, "deadline-attempt", "worker-a", 2).unwrap();
        database.commit(claim).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(&database, &mut bind, "deadline-attempt", "task-a", "worker-a", 6).unwrap();
        database.commit(bind).unwrap();
        let mut start = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_start(&database, &mut start, "deadline-attempt", "task-a", 7).unwrap();
        database.commit(start).unwrap();
        let mut projection_event = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_attempt_event(
            &database,
            &mut projection_event,
            AttemptEventAppend {
                conversation_id: "deadline-conversation",
                turn_id: "deadline-turn",
                attempt_id: "deadline-attempt",
                projection_revision: 3,
                event_type: "projection_updated",
                payload: json!({
                    "projectionPatch": {
                        "version": 1,
                        "baseRevision": 2,
                        "targetRevision": 3,
                        "operations": []
                    }
                }),
                occurred_at_ms: 8,
                publish_conversation_sync: false,
            },
        )
        .unwrap();
        database.commit(projection_event).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let full: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "deadline-attempt", 0, 5_000, true, &mut || false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(full.as_array().unwrap().len(), 3);
        let mut checks = 0;
        let partial: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "deadline-attempt", 0, 5_000, true, &mut || {
                checks += 1;
                checks > 2
            })
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        let events = partial.as_array().unwrap();
        assert_eq!(events.len(), 2);
        assert_eq!(events[0]["seq"], json!(1));
        assert_eq!(events[1]["seq"], json!(2));
    }

    #[test]
    fn events_list_without_budget_is_deadline_elapsed_not_an_empty_page() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "deadline-conversation",
            "deadline-turn",
            "deadline-attempt",
        );
        database.commit(seed).unwrap();
        let mut claim = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_claim(&database, &mut claim, "deadline-attempt", "worker-a", 2).unwrap();
        database.commit(claim).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(&database, &mut bind, "deadline-attempt", "task-a", "worker-a", 6).unwrap();
        database.commit(bind).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            events_list(&database, &mut read, "deadline-attempt", 0, 5_000, true, &mut || false)
                .unwrap()
                .map(|bytes| serde_json::from_slice::<Value>(&bytes).unwrap())
                .as_ref()
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(1)
        );
        let error = events_list(
            &database,
            &mut read,
            "deadline-attempt",
            0,
            5_000,
            true,
            &mut || true,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn sync_page_returns_resumable_window_when_the_deadline_elapses() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (turn, attempt) in [
            ("page-turn-1", "page-attempt-1"),
            ("page-turn-2", "page-attempt-2"),
            ("page-turn-3", "page-attempt-3"),
        ] {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(&database, &mut seed, "page-conversation", turn, attempt);
            database.commit(seed).unwrap();
        }
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let head = sync_head(&database, &mut read, "page-conversation").unwrap();
        // Two turn materializations pass, the third reports the deadline
        // spent: the page keeps the newest turns already read and marks the
        // window resumable at the oldest retained ordinal.
        let mut checks = 0;
        let page: Value = serde_json::from_slice(
            &sync_page(
                &database,
                &mut read,
                "page-conversation",
                "main",
                u64::MAX,
                3,
                head,
                &mut || {
                    checks += 1;
                    checks == 3
                },
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(page["hasMore"], json!(true));
        assert_eq!(page["nextBeforeOrdinal"], json!(1));
        let turns = page["turns"].as_array().unwrap();
        assert_eq!(turns.len(), 2);
        assert_eq!(turns[0]["ordinal"], json!(1));
        assert_eq!(turns[1]["ordinal"], json!(2));
        assert_eq!(page["attempts"].as_array().unwrap().len(), 2);
        // Resuming at the returned cursor drains the older turns exactly once.
        let rest: Value = serde_json::from_slice(
            &sync_page(
                &database,
                &mut read,
                "page-conversation",
                "main",
                1,
                3,
                head,
                &mut || false,
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(rest["hasMore"], json!(false));
        let turns = rest["turns"].as_array().unwrap();
        assert_eq!(turns.len(), 1);
        assert_eq!(turns[0]["ordinal"], json!(0));
    }

    #[test]
    fn sync_snapshot_window_truncates_and_full_list_fails_loud() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (turn, attempt) in [
            ("snapshot-turn-1", "snapshot-attempt-1"),
            ("snapshot-turn-2", "snapshot-attempt-2"),
            ("snapshot-turn-3", "snapshot-attempt-3"),
        ] {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(&database, &mut seed, "snapshot-conversation", turn, attempt);
            database.commit(seed).unwrap();
        }
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        // Windowed snapshot: the tail window truncates to the newest turns
        // and stays resumable through `turnWindow.nextBeforeOrdinal`.
        let mut checks = 0;
        let snapshot: Value = serde_json::from_slice(
            &sync_snapshot(
                &database,
                &mut read,
                "snapshot-conversation",
                3,
                false,
                &mut || {
                    checks += 1;
                    checks == 3
                },
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(snapshot["turnWindow"]["hasMore"], json!(true));
        assert_eq!(snapshot["turnWindow"]["nextBeforeOrdinal"], json!(1));
        assert_eq!(snapshot["turns"].as_array().unwrap().len(), 2);
        // Unwindowed (full-list) snapshot has no resumption cursor: with the
        // deadline spent it must fail as `deadline_elapsed`, never return a
        // silently incomplete history.
        let error = sync_snapshot(
            &database,
            &mut read,
            "snapshot-conversation",
            0,
            false,
            &mut || true,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn sync_snapshot_attempt_materialization_failure_is_loud() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (turn, attempt) in [
            ("attempts-turn-1", "attempts-attempt-1"),
            ("attempts-turn-2", "attempts-attempt-2"),
        ] {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(&database, &mut seed, "attempts-conversation", turn, attempt);
            database.commit(seed).unwrap();
        }
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        // The lane window materializes two turns (two checks pass), then the
        // attempts loop sees the deadline spent: a partial attempt map would
        // corrupt the snapshot silently, so the operation fails instead.
        let mut checks = 0;
        let error = sync_snapshot(
            &database,
            &mut read,
            "attempts-conversation",
            2,
            false,
            &mut || {
                checks += 1;
                checks > 2
            },
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn legacy_messages_windowed_read_matches_full_lane_read() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..8 {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(
                &database,
                &mut seed,
                "window-conversation",
                &format!("window-turn-{index}"),
                &format!("window-attempt-{index}"),
            );
            database.commit(seed).unwrap();
        }
        for (window, before) in [
            (1_usize, None),
            (3, None),
            (8, None),
            (100, None),
            (3, Some(0)),
            (3, Some(2)),
            (3, Some(5)),
            (3, Some(100)),
            (2, Some(-4)),
        ] {
            let mut fast_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let fast = legacy_messages(
                &database,
                &mut fast_read,
                "window-conversation",
                window,
                before,
                &mut || false,
            )
            .unwrap();
            let mut full_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let full = legacy_messages_full(
                &database,
                &mut full_read,
                "window-conversation",
                window,
                before,
                &mut || false,
            )
            .unwrap();
            assert_eq!(fast, full, "window={window} before={before:?}");
        }
    }

    #[test]
    fn legacy_messages_falls_back_to_the_full_read_on_count_disagreement() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..4 {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(
                &database,
                &mut seed,
                "fallback-conversation",
                &format!("fallback-turn-{index}"),
                &format!("fallback-attempt-{index}"),
            );
            database.commit(seed).unwrap();
        }
        let read_full = |database: &AuthorityDatabase| {
            let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            legacy_messages_full(
                database,
                &mut read,
                "fallback-conversation",
                2,
                None,
                &mut || false,
            )
            .unwrap()
        };
        let read_fast = |database: &AuthorityDatabase| {
            let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            legacy_messages(
                database,
                &mut read,
                "fallback-conversation",
                2,
                None,
                &mut || false,
            )
            .unwrap()
        };
        let expected = read_full(&database);
        assert_eq!(read_fast(&database), expected);

        // Missing count (pre-count lane): exact legacy full read.
        let mut tamper = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let count_key = lane_count_key(&tamper, "fallback-conversation", "main").unwrap();
        database.entity_delete(&mut tamper, count_key).unwrap();
        database.commit(tamper).unwrap();
        assert_eq!(read_fast(&database), expected);

        // Understated count (lane longer than the count claims): the probe
        // detects the extra row and falls back.
        let mut tamper = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let count_key = lane_count_key(&tamper, "fallback-conversation", "main").unwrap();
        database
            .entity_put(&mut tamper, count_key, 1_u64.to_le_bytes().to_vec())
            .unwrap();
        database.commit(tamper).unwrap();
        assert_eq!(read_fast(&database), expected);
    }

    #[test]
    fn legacy_messages_overstated_count_beyond_the_window_falls_back() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..4 {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_live_attempt(
                &database,
                &mut seed,
                "overstated-conversation",
                &format!("overstated-turn-{index}"),
                &format!("overstated-attempt-{index}"),
            );
            database.commit(seed).unwrap();
        }
        // Count claims 100 rows but the lane has 4; paging with
        // before_sequence=3 asks for rows the scan cannot supply.
        let mut tamper = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let count_key = lane_count_key(&tamper, "overstated-conversation", "main").unwrap();
        database
            .entity_put(&mut tamper, count_key, 100_u64.to_le_bytes().to_vec())
            .unwrap();
        database.commit(tamper).unwrap();
        let mut fast_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let fast = legacy_messages(
            &database,
            &mut fast_read,
            "overstated-conversation",
            2,
            Some(3),
            &mut || false,
        )
        .unwrap();
        let mut full_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let full = legacy_messages_full(
            &database,
            &mut full_read,
            "overstated-conversation",
            2,
            Some(3),
            &mut || false,
        )
        .unwrap();
        assert_eq!(fast, full);
    }

    #[test]
    fn attempt_claim_is_owner_fenced_replayable_and_occ_serialized() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "claim-conversation",
            "claim-turn",
            "claim-attempt",
        );
        database.commit(seed).unwrap();

        let mut foreign = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut foreign, "claim-attempt", "worker-a", 2).unwrap(),
            b"false"
        );

        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let mut racing = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut first, "claim-attempt", "worker-a", 2).unwrap(),
            b"true"
        );
        assert_eq!(
            attempt_claim(&database, &mut racing, "claim-attempt", "worker-b", 2).unwrap(),
            b"true"
        );
        database.commit(first).unwrap();
        assert!(database.commit(racing).is_err());

        let mut replay = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut replay, "claim-attempt", "worker-a", 3).unwrap(),
            b"true"
        );
        assert_eq!(
            attempt_claim(&database, &mut replay, "claim-attempt", "worker-b", 3).unwrap(),
            b"false"
        );
        drop(replay);
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let bound: Value = serde_json::from_slice(
            &attempt_bind(
                &database,
                &mut bind,
                "claim-attempt",
                "task-a",
                "worker-a",
                6,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(bound["taskId"], "task-a");
        assert_eq!(bound["status"], "pending");
        database.commit(bind).unwrap();

        let mut start = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let running: Value = serde_json::from_slice(
            &attempt_start(&database, &mut start, "claim-attempt", "task-a", 7).unwrap(),
        )
        .unwrap();
        assert_eq!(running["status"], "running");
        database.commit(start).unwrap();

        let mut projection_event = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_attempt_event(
            &database,
            &mut projection_event,
            AttemptEventAppend {
                conversation_id: "claim-conversation",
                turn_id: "claim-turn",
                attempt_id: "claim-attempt",
                projection_revision: 3,
                event_type: "projection_updated",
                payload: json!({
                    "projectionPatch": {
                        "version": 1,
                        "baseRevision": 2,
                        "targetRevision": 3,
                        "operations": []
                    }
                }),
                occurred_at_ms: 8,
                publish_conversation_sync: false,
            },
        )
        .unwrap();
        database.commit(projection_event).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let turn: Value = serde_json::from_slice(
            &get(&database, &mut read, "claim-conversation", "claim-turn")
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(turn["status"], "running");
        assert_eq!(turn["projectionRevision"], 3);
        assert_eq!(
            sync_head(&database, &mut read, "claim-conversation").unwrap(),
            3
        );
        let event_head_key = attempt_event_head_key(&read, "claim-attempt").unwrap();
        assert_eq!(
            decode_u64(
                database.entity_get(&mut read, &event_head_key).unwrap(),
                "attempt event head is malformed",
            )
            .unwrap(),
            3
        );
        let events: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "claim-attempt", 0, 5_000, true, &mut || false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(events.as_array().unwrap().len(), 3);
        assert_eq!(events[0]["payload"]["dispatchState"], "queued");
        assert_eq!(events[1]["payload"]["dispatchState"], "running");
        assert_eq!(events[2]["type"], "projection_updated");
        let page: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "claim-attempt", 0, 1, false, &mut || false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(page.as_array().unwrap().len(), 1);
        assert_eq!(page[0]["seq"], 1);
        let hydrated: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "claim-attempt", 2, 1, false, &mut || false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(hydrated[0]["payload"]["projection"]["content"], "");
        drop(read);

        let mut foreign_read = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert_eq!(
            events_list(
                &database,
                &mut foreign_read,
                "claim-attempt",
                0,
                1_000,
                true,
                &mut || false,
            )
            .unwrap(),
            None
        );
        drop(foreign_read);

        let mut legacy_seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut legacy_seed,
            "legacy-claim-conversation",
            "legacy-claim-turn",
            "legacy-claim-attempt",
        );
        database.commit(legacy_seed).unwrap();
        let mut legacy = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut legacy, "legacy-claim-attempt", "", 4).unwrap(),
            b"true"
        );
        database.commit(legacy).unwrap();
        let mut legacy_replay = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut legacy_replay, "legacy-claim-attempt", "", 5).unwrap(),
            b"false"
        );
    }

    #[test]
    fn activity_index_avoids_projection_results_and_legacy_fallback_is_all_or_nothing() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut write,
            &crate::conversation_header::CreateRequest {
                conversation_id: "activity-conversation".to_owned(),
                title: "Activity".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        for (ordinal, timestamp) in [100_u64, 200].into_iter().enumerate() {
            append_settled(
                &database,
                &mut write,
                &AppendSettledRequest {
                    conversation_id: "activity-conversation".to_owned(),
                    actor: "human".to_owned(),
                    status: "completed".to_owned(),
                    projection_json: serde_json::to_vec(&json!({
                        "content": "x".repeat(20_000),
                        "timestamp": timestamp,
                    }))
                    .unwrap(),
                    projection_timestamp: None,
                    settlement_json: b"{}".to_vec(),
                    lane_id: "main".to_owned(),
                    command_id: format!("activity-command-{ordinal}"),
                    kind: "fixture".to_owned(),
                    run_id: String::new(),
                    turn_id: format!("activity-turn-{ordinal}"),
                    attempt_id: None,
                    created_at_ms: timestamp,
                    committed_at_ms: timestamp,
                    defaults: TurnDefaults {
                        allow_create: false,
                        title: String::new(),
                        settings_json: b"{}".to_vec(),
                        created_at_ms: timestamp,
                    },
                },
            )
            .unwrap();
        }
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let mut remaining = crate::generated_tofudb_ir::MAX_ACTIVITY_TURN_ROWS_PER_QUERY;
        assert_eq!(
            activity_intervals(
                &database,
                &mut read,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap(),
            BTreeSet::from([0, 1])
        );
        assert_eq!(
            remaining,
            crate::generated_tofudb_ir::MAX_ACTIVITY_TURN_ROWS_PER_QUERY - 2
        );
        drop(read);
        let mut bounded = database.begin(7, 11).unwrap();
        let mut remaining = 1;
        assert_eq!(
            activity_intervals(
                &database,
                &mut bounded,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::OutOfMemory
        );
        drop(bounded);

        let mut remove = database.begin(7, 11).unwrap();
        for ordinal in 0..2 {
            let key = activity_index_key(&remove, "activity-conversation", ordinal).unwrap();
            database.entity_delete(&mut remove, key).unwrap();
        }
        database.commit(remove).unwrap();
        let mut legacy = database.begin(7, 11).unwrap();
        let mut remaining = 2;
        assert_eq!(
            activity_intervals(
                &database,
                &mut legacy,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap(),
            BTreeSet::from([0, 1])
        );
        drop(legacy);

        let mut partial = database.begin(7, 11).unwrap();
        let partial_key = activity_index_key(&partial, "activity-conversation", 0).unwrap();
        database
            .entity_put(&mut partial, partial_key, encode_activity_index_value(100))
            .unwrap();
        database.commit(partial).unwrap();
        let mut corrupt = database.begin(7, 11).unwrap();
        let mut remaining = 10;
        assert_eq!(
            activity_intervals(
                &database,
                &mut corrupt,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn attempt_locator_is_versioned_bounded_and_legacy_read_compatible() {
        let encoded = encode_attempt_locator(11, "conversation").unwrap();
        match decode_attempt_locator(&encoded).unwrap() {
            AttemptLocator::Conversation {
                owner_user_id,
                conversation_id,
            } => {
                assert_eq!(owner_user_id, 11);
                assert_eq!(conversation_id, "conversation");
            }
            AttemptLocator::LegacyOwner(_) => panic!("new locator used legacy encoding"),
        }
        assert!(decode_attempt_locator(&encoded[..encoded.len() - 1]).is_err());
        let legacy = 11_u64.to_be_bytes();
        assert!(matches!(
            decode_attempt_locator(&legacy).unwrap(),
            AttemptLocator::LegacyOwner(11)
        ));
        assert!(decode_attempt_locator(&0_u64.to_be_bytes()).is_err());

        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let transaction = database.begin(7, 11).unwrap();
        let clustered = attempt_key(&transaction, "conversation", "attempt").unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            GENERATION_ATTEMPT_NAMESPACE,
            &conversation_prefix("conversation").unwrap(),
        )
        .unwrap();
        assert!(start <= clustered && clustered < end);
        let legacy = legacy_attempt_key(&transaction, "attempt").unwrap();
        assert!(legacy < start || legacy >= end);
        drop(transaction);

        let legacy_attempt = json!({
            "attemptId": "legacy-attempt",
            "conversationId": "legacy-conversation",
            "turnId": "legacy-turn",
            "status": "completed"
        });
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut write,
            &crate::conversation_header::CreateRequest {
                conversation_id: "legacy-conversation".to_owned(),
                title: "Legacy".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        let legacy_document_key = legacy_attempt_key(&write, "legacy-attempt").unwrap();
        crate::versioned_document::put(
            &database,
            &mut write,
            crate::versioned_document::PutRequest {
                key: legacy_document_key,
                namespace: "generation_attempts".to_owned(),
                logical_key: "legacy-attempt".to_owned(),
                value_json: serde_json::to_vec(&legacy_attempt).unwrap(),
                expected_version: Some(0),
                updated_at_ms: 1,
            },
        )
        .unwrap();
        let claim = global_identity_claim_key(&write, ATTEMPT_ID_CLAIM_NAMESPACE, "legacy-attempt")
            .unwrap();
        database
            .entity_put(&mut write, claim, 11_u64.to_be_bytes().to_vec())
            .unwrap();
        database.commit(write).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &attempt_get(&database, &mut read, "legacy-attempt")
                    .unwrap()
                    .unwrap()
            )
            .unwrap(),
            legacy_attempt
        );
        drop(read);
        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &mut database,
            &mut delete,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "legacy-conversation".to_owned(),
                deleted_at_ms: 2,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_get(&database, &mut read, "legacy-attempt").unwrap(),
            None
        );
    }

    #[test]
    fn stale_execution_epoch_projects_a_fully_inert_turn() {
        let projected = public_turn(
            json!({
                "turnId": "turn",
                "conversationId": "conversation",
                "status": "running",
                "runId": "live-run",
                "currentAttemptId": "live-attempt",
                "_executionEpoch": 3,
                "projectionRevision": 1,
                "projection": {
                    "isStreaming": true,
                    "nested": {"attemptId": "nested-attempt", "content": "keep"},
                    "toolRounds": [{"status": "running"}, {"status": "completed"}],
                    "segments": [{
                        "type": "tool_use",
                        "result": {"status": "running", "content": "keep"}
                    }],
                    "timingTrace": {"status": "running", "running": true}
                },
                "settlement": {}
            })
            .as_object()
            .unwrap()
            .clone(),
            4,
        )
        .unwrap();
        assert_eq!(projected["status"], "interrupted");
        assert_eq!(projected["runId"], "");
        assert_eq!(projected["currentAttemptId"], Value::Null);
        assert_eq!(projected["settlement"]["cause"], "conversation_deleted");
        assert!(projected.get("_executionEpoch").is_none());
        assert!(projected["projection"].get("isStreaming").is_none());
        assert!(projected["projection"]["nested"].get("attemptId").is_none());
        assert_eq!(projected["projection"]["nested"]["content"], "keep");
        assert_eq!(
            projected["projection"]["toolRounds"][0]["status"],
            "aborted"
        );
        assert_eq!(
            projected["projection"]["toolRounds"][1]["status"],
            "completed"
        );
        assert_eq!(
            projected["projection"]["segments"][0]["result"]["status"],
            "aborted"
        );
        assert_eq!(projected["projection"]["timingTrace"]["status"], "aborted");
        assert_eq!(projected["projection"]["timingTrace"]["running"], false);
    }

    #[test]
    fn delta_scan_refuses_an_incomplete_page_above_its_row_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        for index in 0..=MAX_DELTA_ROWS {
            let turn_id = format!("turn-{index:04}");
            let key = updated_index_key(&write, "conversation", index as u64, &turn_id).unwrap();
            database
                .entity_put(
                    &mut write,
                    key,
                    encode_updated_index_value(&turn_id, 1).unwrap(),
                )
                .unwrap();
        }
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            list_delta(&database, &mut read, "conversation", 0, &BTreeMap::new(), 1,)
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    #[test]
    fn delta_revision_index_codec_rejects_truncation_and_trailing_bytes() {
        let encoded = encode_updated_index_value("turn-a", 7).unwrap();
        assert_eq!(
            decode_updated_index_value(&encoded).unwrap(),
            ("turn-a".to_owned(), 7)
        );
        assert!(decode_updated_index_value(&encoded[..encoded.len() - 1]).is_err());
        let mut trailing = encoded;
        trailing.push(0);
        assert!(decode_updated_index_value(&trailing).is_err());
    }

    #[test]
    fn tombstone_pruning_is_bounded_and_releases_only_matching_identity_claims() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in 0..=MAX_TOMBSTONE_PRUNE_ROWS {
            let turn_id = format!("old-{index:03}");
            let attempt_id = format!("attempt-{index:03}");
            let tombstone_key = tombstone_index_key(&write, "conversation", 1, &turn_id).unwrap();
            let age_key = tombstone_age_index_key(&write, 1, "conversation", &turn_id).unwrap();
            let turn_claim_key =
                global_identity_claim_key(&write, TURN_ID_CLAIM_NAMESPACE, &turn_id).unwrap();
            let attempt_claim_key =
                global_identity_claim_key(&write, ATTEMPT_ID_CLAIM_NAMESPACE, &attempt_id).unwrap();
            database
                .entity_put(&mut write, tombstone_key, Vec::new())
                .unwrap();
            database
                .entity_put(&mut write, age_key, attempt_id.as_bytes().to_vec())
                .unwrap();
            database
                .entity_put(&mut write, turn_claim_key, 11_u64.to_be_bytes().to_vec())
                .unwrap();
            database
                .entity_put(&mut write, attempt_claim_key, 11_u64.to_be_bytes().to_vec())
                .unwrap();
        }
        let fresh_turn_id = "fresh";
        let fresh_at = TOMBSTONE_RETENTION_MS + 100;
        let fresh_tombstone_key =
            tombstone_index_key(&write, "conversation", fresh_at, fresh_turn_id).unwrap();
        let fresh_age_key =
            tombstone_age_index_key(&write, fresh_at, "conversation", fresh_turn_id).unwrap();
        let fresh_claim_key =
            global_identity_claim_key(&write, TURN_ID_CLAIM_NAMESPACE, fresh_turn_id).unwrap();
        database
            .entity_put(&mut write, fresh_tombstone_key, Vec::new())
            .unwrap();
        database
            .entity_put(&mut write, fresh_age_key, Vec::new())
            .unwrap();
        database
            .entity_put(&mut write, fresh_claim_key, 11_u64.to_be_bytes().to_vec())
            .unwrap();
        database.commit(write).unwrap();

        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            prune_expired_tombstones(&database, &mut prune, fresh_at).unwrap(),
            MAX_TOMBSTONE_PRUNE_ROWS
        );
        database.commit(prune).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let (start, end) =
            EntityKey::prefix_range(7, 11, TURN_TOMBSTONE_AGE_INDEX_NAMESPACE, b"").unwrap();
        assert_eq!(
            database
                .entity_scan(&mut read, &start, &end, 10)
                .unwrap()
                .len(),
            2
        );
        let removed_claim_key =
            global_identity_claim_key(&read, TURN_ID_CLAIM_NAMESPACE, "old-000").unwrap();
        let retained_stale_claim_key =
            global_identity_claim_key(&read, TURN_ID_CLAIM_NAMESPACE, "old-256").unwrap();
        let retained_fresh_claim_key =
            global_identity_claim_key(&read, TURN_ID_CLAIM_NAMESPACE, fresh_turn_id).unwrap();
        assert!(database
            .entity_get(&mut read, &removed_claim_key)
            .unwrap()
            .is_none());
        assert!(database
            .entity_get(&mut read, &retained_stale_claim_key)
            .unwrap()
            .is_some());
        assert!(database
            .entity_get(&mut read, &retained_fresh_claim_key)
            .unwrap()
            .is_some());
    }

    #[test]
    fn delta_known_revision_skips_document_and_blob_materialization() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        let index_key = updated_index_key(&write, "conversation", 10, "unchanged").unwrap();
        database
            .entity_put(
                &mut write,
                index_key,
                encode_updated_index_value("unchanged", 9).unwrap(),
            )
            .unwrap();
        database.commit(write).unwrap();

        let mut known_revisions = BTreeMap::new();
        known_revisions.insert("unchanged".to_owned(), 9);
        let mut read = database.begin(7, 11).unwrap();
        let response = list_delta(
            &database,
            &mut read,
            "conversation",
            15,
            &known_revisions,
            20,
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&response).unwrap(),
            json!({"turns": [], "deletedTurnIds": [], "serverNowMs": 20})
        );
    }

    #[test]
    fn delta_response_budget_rejects_multiple_large_blob_turns() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let count = crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES / 4_200_000 + 1;
        for ordinal in 0..count as u64 {
            let identity = format!("large-{ordinal}");
            let turn_id = identity.as_str();
            let mut write = database.begin(7, 11).unwrap();
            let document = json!({
                "turnId": turn_id,
                "presentationId": turn_id,
                "conversationId": "conversation",
                "laneId": "main",
                "parentTurnId": Value::Null,
                "ordinal": ordinal,
                "actor": "assistant",
                "kind": "fixture",
                "runId": "",
                "status": "completed",
                "currentAttemptId": Value::Null,
                "projection": {"content": "x".repeat(4_200_000)},
                "projectionRevision": 1,
                "settlement": {},
                "createdAt": ordinal + 1,
                "updatedAt": ordinal + 1
            });
            let document_key = turn_key(&write, "conversation", turn_id).unwrap();
            crate::versioned_document::put(
                &database,
                &mut write,
                crate::versioned_document::PutRequest {
                    key: document_key,
                    namespace: DOCUMENT_IDENTITY.to_owned(),
                    logical_key: turn_logical_key("conversation", turn_id),
                    value_json: serde_json::to_vec(&document).unwrap(),
                    expected_version: Some(0),
                    updated_at_ms: ordinal + 1,
                },
            )
            .unwrap();
            let index_key =
                updated_index_key(&write, "conversation", ordinal + 1, turn_id).unwrap();
            database
                .entity_put(
                    &mut write,
                    index_key,
                    encode_updated_index_value(turn_id, 1).unwrap(),
                )
                .unwrap();
            database.commit(write).unwrap();
        }

        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            list_delta(
                &database,
                &mut read,
                "conversation",
                0,
                &BTreeMap::new(),
                10,
            )
            .err()
            .expect("aggregate Turn delta must stay within its response budget")
            .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    #[test]
    fn attempt_directory_and_tombstone_bounds_are_explicit_and_legacy_compatible() {
        let attempt_ids = (0..MAX_ATTEMPTS_PER_TURN)
            .map(|index| format!("attempt-{index:03}-{}", "x".repeat(112)))
            .collect::<Vec<_>>();
        let encoded = encode_tombstone_attempt_ids(&attempt_ids).unwrap();
        assert!(encoded.len() <= MAX_ATTEMPT_TOMBSTONE_BYTES);
        assert_eq!(decode_tombstone_attempt_ids(&encoded).unwrap(), attempt_ids);
        assert_eq!(
            decode_tombstone_attempt_ids(b"legacy-attempt").unwrap(),
            vec!["legacy-attempt"]
        );
        let mut over_limit = attempt_ids;
        over_limit.push("one-too-many".to_owned());
        assert_eq!(
            encode_tombstone_attempt_ids(&over_limit)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );

        let maximum_ascii_entries = (0..MAX_ATTEMPTS_PER_TURN)
            .map(|index| {
                Map::from_iter([
                    (
                        "attemptId".to_owned(),
                        Value::String(format!("attempt-{index:03}-{}", "a".repeat(112))),
                    ),
                    ("conversationId".to_owned(), Value::String("c".repeat(256))),
                    ("turnId".to_owned(), Value::String("t".repeat(128))),
                    ("taskId".to_owned(), Value::String("k".repeat(256))),
                    ("createdAt".to_owned(), Value::from(u64::MAX)),
                    ("effectiveAt".to_owned(), Value::from(u64::MAX)),
                    (
                        "dispatchMode".to_owned(),
                        Value::String("conversation_executor".to_owned()),
                    ),
                ])
            })
            .collect::<Vec<_>>();
        let maximum_ascii_directory = serde_json::to_vec(&maximum_ascii_entries).unwrap();
        assert!(maximum_ascii_directory.len() <= MAX_ATTEMPT_TURN_DIRECTORY_BYTES);
        assert_eq!(
            decode_attempt_turn_directory(&maximum_ascii_directory)
                .unwrap()
                .len(),
            MAX_ATTEMPTS_PER_TURN
        );

        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            load_attempt_identity_ids(
                &database,
                &mut transaction,
                "legacy-conversation",
                "legacy-turn",
                Some("legacy-attempt"),
            )
            .unwrap(),
            vec!["legacy-attempt"]
        );
        let entries = (0..=MAX_ATTEMPTS_PER_TURN)
            .map(|_| Map::new())
            .collect::<Vec<_>>();
        assert_eq!(
            store_attempt_turn_directory(
                &database,
                &mut transaction,
                "conversation",
                "turn",
                &entries,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    #[test]
    fn turn_delete_cascades_every_historical_attempt_and_releases_all_claims_after_retention() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "cascade-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"settled"}"#.to_vec(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "cascade-first-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "cascade-turn".to_owned(),
                attempt_id: Some("cascade-first".to_owned()),
                created_at_ms: 10,
                committed_at_ms: 10,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Cascade".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 10,
                },
            },
        )
        .unwrap();
        append_historical_attempt(
            &database,
            &mut seed,
            "cascade-conversation",
            "cascade-turn",
            "cascade-first",
            "cascade-second",
            20,
        );
        database.commit(seed).unwrap();

        let mut remove = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        delete(
            &mut database,
            &mut remove,
            "cascade-conversation",
            &["cascade-turn".to_owned()],
            100,
        )
        .unwrap();
        database.commit(remove).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(attempt_get(&database, &mut read, "cascade-first")
            .unwrap()
            .is_none());
        assert!(attempt_get(&database, &mut read, "cascade-second")
            .unwrap()
            .is_none());
        assert!(
            events_list(&database, &mut read, "cascade-second", 0, 10, true, &mut || false)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            serde_json::from_slice::<Value>(
                &timing_trace_list(&database, &mut read, "cascade-conversation", None, 10,)
                    .unwrap(),
            )
            .unwrap(),
            json!({"records": [], "has_more": false})
        );
        for attempt_id in ["cascade-first", "cascade-second"] {
            let claim_key =
                global_identity_claim_key(&read, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id).unwrap();
            assert!(database
                .entity_get(&mut read, &claim_key)
                .unwrap()
                .is_some());
        }
        drop(read);

        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            prune_expired_tombstones(&database, &mut prune, TOMBSTONE_RETENTION_MS + 101).unwrap(),
            1
        );
        database.commit(prune).unwrap();
        let mut final_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for (namespace, identity) in [
            (TURN_ID_CLAIM_NAMESPACE, "cascade-turn"),
            (ATTEMPT_ID_CLAIM_NAMESPACE, "cascade-first"),
            (ATTEMPT_ID_CLAIM_NAMESPACE, "cascade-second"),
        ] {
            let claim_key = global_identity_claim_key(&final_read, namespace, identity).unwrap();
            assert!(database
                .entity_get(&mut final_read, &claim_key)
                .unwrap()
                .is_none());
        }
    }

    #[test]
    fn restored_conversation_retains_inert_history_for_complete_purge() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "restore-history".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"settled"}"#.to_vec(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "restore-first-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "restore-turn".to_owned(),
                attempt_id: Some("restore-first".to_owned()),
                created_at_ms: 10,
                committed_at_ms: 10,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Restore history".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 10,
                },
            },
        )
        .unwrap();
        append_historical_attempt(
            &database,
            &mut seed,
            "restore-history",
            "restore-turn",
            "restore-first",
            "restore-second",
            20,
        );
        database.commit(seed).unwrap();

        let mut trash = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &mut database,
            &mut trash,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "restore-history".to_owned(),
                deleted_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(trash).unwrap();
        let mut restore = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::restore(
            &mut database,
            &mut restore,
            &crate::conversation_header::RestoreRequest {
                conversation_id: "restore-history".to_owned(),
                committed_at_ms: 200,
            },
        )
        .unwrap();
        database.commit(restore).unwrap();

        let mut inspect = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            load_attempt_turn_directory(
                &database,
                &mut inspect,
                "restore-history",
                "restore-turn",
                Some("restore-first"),
            )
            .unwrap()
            .len(),
            2
        );
        for attempt_id in ["restore-first", "restore-second"] {
            assert!(attempt_get(&database, &mut inspect, attempt_id)
                .unwrap()
                .is_none());
        }
        drop(inspect);

        let mut purge = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::purge(
            &mut database,
            &mut purge,
            &crate::conversation_header::PurgeRequest {
                conversation_id: "restore-history".to_owned(),
                purged_at_ms: 300,
            },
        )
        .unwrap();
        database.commit(purge).unwrap();
        let mut final_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for attempt_id in ["restore-first", "restore-second"] {
            let claim_key =
                global_identity_claim_key(&final_read, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)
                    .unwrap();
            assert!(database
                .entity_get(&mut final_read, &claim_key)
                .unwrap()
                .is_none());
        }
    }

    fn seed_settled_attempt_with_events(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        turn_id: &str,
        attempt_id: &str,
        settled_at_ms: u64,
        event_count: usize,
    ) {
        // The real settled-append path creates the conversation header, the
        // turn, the terminal attempt document, and its retention index row.
        append_settled(
            database,
            transaction,
            &AppendSettledRequest {
                conversation_id: conversation_id.to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"seeded"}"#.to_vec(),
                projection_timestamp: None,
                                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("command-{attempt_id}"),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: Some(attempt_id.to_owned()),
                created_at_ms: settled_at_ms,
                committed_at_ms: settled_at_ms,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Prune fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: settled_at_ms,
                },
            },
        )
        .unwrap();
        // Raw streaming chunks (the cold-tier payload the prune retires).
        for index in 0..event_count {
            append_attempt_event(
                database,
                transaction,
                AttemptEventAppend {
                    conversation_id,
                    turn_id,
                    attempt_id,
                    projection_revision: 1,
                    event_type: "text-delta",
                    payload: Value::String(format!("chunk-{index}")),
                    occurred_at_ms: settled_at_ms,
                    publish_conversation_sync: false,
                },
            )
            .unwrap();
        }
    }

    fn pruned_event_count(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        attempt_id: &str,
    ) -> usize {
        let (start, end) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            ATTEMPT_EVENT_NAMESPACE,
            &attempt_event_prefix(attempt_id).unwrap(),
        )
        .unwrap();
        database
            .entity_scan(transaction, &start, &end, 1_000)
            .unwrap()
            .len()
    }

    #[test]
    fn scheduled_attempt_event_prune_bounds_the_round_and_resumes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let now_ms = 1_800_000_000_000_u64;
        let old_ms = now_ms - 7 * 24 * 60 * 60 * 1_000 - 1_000;
        // Attempt A: 1001 events (one over the per-attempt scan bound);
        // attempt B: 1000 (exactly at it, conservatively unfinished). The
        // 8192-row request covers both in one round via range retirement.
        for (attempt, count) in [("big-attempt-a", 1001), ("big-attempt-b", 1000)] {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            seed_settled_attempt_with_events(
                &database,
                &mut seed,
                "prune-conversation",
                &format!("prune-turn-{attempt}"),
                attempt,
                old_ms,
                count,
            );
            database.commit(seed).unwrap();
        }
        let request = EventPruneRequest {
            settled_before_ms: now_ms - 7 * 24 * 60 * 60 * 1_000,
            max_attempts: 64,
            max_rows: 8_192,
        };
        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let outcome: Value = serde_json::from_slice(
            &events_prune(&database, &mut prune, &request).unwrap(),
        )
        .unwrap();
        assert_eq!(
            outcome,
            json!({"deleted_rows": 2001, "deleted_attempts": 2, "remaining": 0})
        );
        database.commit(prune).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(pruned_event_count(&database, &mut read, "big-attempt-a"), 0);
        assert_eq!(pruned_event_count(&database, &mut read, "big-attempt-b"), 0);
    }

    #[test]
    fn scheduled_attempt_event_prune_keeps_recent_and_sync_referenced() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let now_ms = 1_800_000_000_000_u64;
        let old_ms = now_ms - 7 * 24 * 60 * 60 * 1_000 - 1_000;
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        seed_settled_attempt_with_events(
            &database,
            &mut seed,
            "prune-conversation",
            "prune-turn-old",
            "old-attempt",
            old_ms,
            3,
        );
        seed_settled_attempt_with_events(
            &database,
            &mut seed,
            "prune-conversation",
            "prune-turn-recent",
            "recent-attempt",
            now_ms,
            4,
        );
        database.commit(seed).unwrap();
        let request = EventPruneRequest {
            settled_before_ms: now_ms - 7 * 24 * 60 * 60 * 1_000,
            max_attempts: 64,
            max_rows: 8_192,
        };
        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let outcome: Value = serde_json::from_slice(
            &events_prune(&database, &mut prune, &request).unwrap(),
        )
        .unwrap();
        assert_eq!(
            outcome,
            json!({"deleted_rows": 3, "deleted_attempts": 1, "remaining": 0})
        );
        database.commit(prune).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(pruned_event_count(&database, &mut read, "old-attempt"), 0);
        assert_eq!(pruned_event_count(&database, &mut read, "recent-attempt"), 4);
        // The recent attempt's turn and attempt documents survive pruning.
        let recent_attempt_key =
            attempt_key(&read, "prune-conversation", "recent-attempt").unwrap();
        assert!(database
            .entity_get(&mut read, &recent_attempt_key)
            .unwrap()
            .is_some());
        let prune_turn_key = turn_key(&read, "prune-conversation", "prune-turn-recent").unwrap();
        assert!(database
            .entity_get(&mut read, &prune_turn_key)
            .unwrap()
            .is_some());
    }

// Independent previous borrowed implementation is the wire/error oracle.
#[cfg(test)]
mod materialized_ownership_tests {
    use super::*;
fn borrowed_legacy_message_reference(turn: &Value) -> io::Result<Value> {
    let turn = turn
        .as_object()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let mut message = turn
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    message.remove("role");
    let actor = turn
        .get("actor")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn actor is malformed"))?;
    message.insert(
        "role".to_owned(),
        Value::String(legacy_role(actor).to_owned()),
    );
    for (target, source) in [
        ("_turnId", "turnId"),
        ("_attemptId", "currentAttemptId"),
        ("_turnActor", "actor"),
        ("_turnKind", "kind"),
        ("_turnLaneId", "laneId"),
        ("_turnStatus", "status"),
        ("_turnSettlement", "settlement"),
        ("_projectionRevision", "projectionRevision"),
    ] {
        message.insert(
            target.to_owned(),
            turn.get(source).cloned().unwrap_or(Value::Null),
        );
    }
    message.insert("_commandPending".to_owned(), Value::Null);
    if !message.get("timestamp").is_some_and(python_truthy) {
        message.insert(
            "timestamp".to_owned(),
            turn.get("createdAt").cloned().unwrap_or(Value::from(0)),
        );
    }
    Ok(Value::Object(message))
}

    #[test]
    fn owned_legacy_projection_matches_borrowed_reference_and_preserves_allocations() {
        let hostile = format!("{}é\"\\\n{}", "x".repeat(192 * 1024), "汉字".repeat(64));
        for actor in ["human", "agent", "system", "tool", "unknown"] {
            for timestamp in [Value::Null, json!(false), json!(0), json!(""), json!([]), json!({}), json!(123), json!("present")] {
                let turn = json!({
                    "actor": actor, "turnId": "turn-1", "currentAttemptId": "attempt-1",
                    "kind": "message", "laneId": "main", "status": "completed",
                    "settlement": {"outcome": "completed", "evidence": hostile},
                    "createdAt": 1234, "projectionRevision": 7,
                    "projection": {"content": hostile, "timestamp": timestamp, "role": "old",
                        "nested": {"values": [1, null, true]}, "_turnId": "stale",
                        "_commandPending": "stale"}
                });
                let expected = borrowed_legacy_message_reference(&turn).unwrap();
                let content_allocation = turn["projection"]["content"].as_str().unwrap().as_ptr();
                let settlement_allocation = turn["settlement"]["evidence"].as_str().unwrap().as_ptr();
                let actual = legacy_message(turn).unwrap();
                assert_eq!(actual, expected, "actor={actor}");
                assert_eq!(serde_json::to_vec(&actual).unwrap(), serde_json::to_vec(&expected).unwrap());
                assert_eq!(actual["content"].as_str().unwrap().as_ptr(), content_allocation);
                assert_eq!(actual["_turnSettlement"]["evidence"].as_str().unwrap().as_ptr(), settlement_allocation);
            }
        }
    }

    #[test]
    fn owned_legacy_projection_preserves_missing_fields_and_errors() {
        for turn in [Value::Null, json!([]), json!({}), json!({"projection": null}),
            json!({"projection": [], "actor": "human"}), json!({"projection": {}}),
            json!({"projection": {}, "actor": 7}), json!({"projection": {}, "actor": "human"}),
            json!({"projection": {"timestamp": 42}, "actor": "agent"})] {
            let expected = borrowed_legacy_message_reference(&turn);
            let actual = legacy_message(turn);
            match (actual, expected) {
                (Ok(actual), Ok(expected)) => assert_eq!(actual, expected),
                (Err(actual), Err(expected)) => {
                    assert_eq!(actual.kind(), expected.kind());
                    assert_eq!(actual.to_string(), expected.to_string());
                }
                _ => panic!("owned conversion changed error boundary"),
            }
        }
    }
}

#[cfg(test)]
mod snapshot_response_construction_tests {
    use super::*;

    #[test]
    fn borrowed_snapshot_matches_previous_tree_bytes_and_normalizes_attempts() {
        let settings = Map::from_iter([("hostile".to_owned(), json!("汉字\"\\\n")),
            ("nested".to_owned(), json!({"z": [true, null, 3], "a": {}}))]);
        // Original attempt conversion canonicalizes these valid source bytes.
        let attempts = vec![RawValue::from_string(
            "{ \"z\": 1e0, \"a\": {\"second\":2,\"first\":1} }".to_owned()).unwrap()];
        for content_bytes in [0, 4096] {
            let turns = vec![json!({"ordinal": 9, "projection": {"content": "汉字\"\\\n".repeat(content_bytes)},
                "settlement": {"outcome": "completed"}})];
            for window in [None, Some(json!({"hasMore": true, "laneId": "main", "nextBeforeOrdinal": 9, "totalTurns": 40}))] {
                for hint in [false, true] {
                    let mut previous = json!({"conversationId": "id汉字\"\\\n", "conversationRevision": 12,
                        "syncSequence": 42, "settings": settings, "turns": turns,
                        "attempts": attempts, "queueItems": []});
                    if let Some(value) = &window { previous["turnWindow"] = value.clone(); }
                    if hint { previous["hasArtifacts"] = Value::Bool(false); }
                    let response = SyncSnapshotResponse {attempts: json!(attempts),
                        conversation_id: "id汉字\"\\\n", conversation_revision: 12,
                        has_artifacts: hint.then_some(false), queue_items: &[], settings: &settings,
                        sync_sequence: 42, turn_window: window.as_ref(), turns: &turns};
                    let actual = encode_response(&response).unwrap();
                    assert_eq!(actual, encode_response(&previous).unwrap());
                    let decoded: Value = serde_json::from_slice(&actual).unwrap();
                    assert_eq!(decoded, previous);
                    assert_eq!(decoded["attempts"][0]["z"], json!(1.0));
                    assert_eq!(attempts[0].get(), "{ \"z\": 1e0, \"a\": {\"second\":2,\"first\":1} }");
                }
            }
        }
    }

    #[test]
    fn borrowed_snapshot_retains_the_complete_response_byte_limit() {
        let settings = Map::new();
        let turns = vec![json!({"content": "x".repeat(crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES + 1)})];
        let response = SyncSnapshotResponse {attempts: json!([]), conversation_id: "bounded",
            conversation_revision: 1, has_artifacts: None, queue_items: &[], settings: &settings,
            sync_sequence: 1, turn_window: None, turns: &turns};
        let error = encode_response(&response).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::OutOfMemory);
        assert_eq!(error.to_string(), "turn response exceeds its bounded streamed payload");
    }
}

#[cfg(test)]
mod event_evidence_witness_tests {
    use super::*;
    fn request() -> EventRecordRequest {
        EventRecordRequest {
            model_continuation: Value::Null, attempt_id: "a".to_owned(), task_id: "task".to_owned(),
            terminal: false, status: "running".to_owned(), slim: false,
            content: "汉字\"\\\n".repeat(8192), thinking: "思考\"\\\n".to_owned(),
            projection: Value::Null, projection_patch: None, projection_segments_stable: false,
            settlement: Map::new(), error: Map::new(), event_type: "updated".to_owned(),
            event_payload: Map::new(), background_command_ack: None, task_event: None, preceding_task_events: Vec::new(),
            flow_root_attempt_id: String::new(), now_ms: 42,
        }
    }
    #[test]
    fn evidence_witness_matches_previous_encoded_projection_for_all_modes() {
        let mut request = request();
        // Final terminal trace is part of the tree before its byte witness.
        let projection = Map::from_iter([("content".to_owned(), json!(request.content)),
            ("thinking".to_owned(), json!(request.thinking)),
            ("timingTrace".to_owned(), json!({"latency": 1.25, "nested": [null, true, "汉字"]})),
            ("evidence".to_owned(), json!("x".repeat(192 * 1024)))]);
        let counted = serde_json::to_vec(&projection).unwrap().len();
        for terminal in [false,true] {for slim in [false,true] {for patch in [false,true] {
            request.terminal=terminal;request.slim=slim;request.projection_patch=patch.then(Map::new);
            for text_bytes in [None,Some(123456)] {
                let previous = if slim && !terminal && !patch {
                    json!({"content":request.content,"thinking":request.thinking})
                } else {Value::Object(projection.clone())};
                let expected=text_bytes.unwrap_or_else(||serde_json::to_vec(&previous).unwrap().len());
                assert_eq!(event_projection_evidence_bytes(&request,text_bytes,counted).unwrap(),expected);
            }
        }}}
    }
    #[test]
    fn borrowed_turn_map_storage_encoding_matches_previous_complete_tree() {
        for content in [String::new(),"汉字\"\\\n".repeat(32768)] {
            let turn=Map::from_iter([("projection".to_owned(),json!({"content":content,"nested":[1,true,null]})),
                ("status".to_owned(),json!("completed")),("_executionEpoch".to_owned(),json!(7)),
                ("settlement".to_owned(),json!({"outcome":"completed","metadata":{"z":2,"a":1}}))]);
            assert_eq!(serde_json::to_vec(&turn).unwrap(),serde_json::to_vec(&Value::Object(turn.clone())).unwrap());
        }
    }
}

    fn append_resume_tail_turn(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        turn_id: &str,
        actor: &str,
        lane_id: &str,
        projection: Value,
        status: &str,
    ) {
        let settlement = if turn_id == "target" {
            json!({"outcome": "truncated", "resumeOptions": [
                {"operation": "continue", "anchor": {}},
                {"operation": "answer_guidance", "anchor": {}},
                {"operation": "checkpoint_resume", "anchor": {
                    "content": "", "thinking": "", "segments": [], "keptToolRounds": 1
                }}
            ]})
        } else {
            json!({})
        };
        append_settled(
            database,
            transaction,
            &AppendSettledRequest {
                conversation_id: "resume-tail".to_owned(),
                actor: actor.to_owned(),
                status: status.to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                projection_timestamp: None,
                settlement_json: serde_json::to_vec(&settlement).unwrap(),
                lane_id: lane_id.to_owned(),
                command_id: format!("ingest-{turn_id}"),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: (actor != "human").then(|| format!("seed-{turn_id}")),
                created_at_ms: 100,
                committed_at_ms: 100,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Resume tail fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 100,
                },
            },
        )
        .unwrap();
    }

    fn seed_resume_tail(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        live_kind: Option<&str>,
    ) {
        for (turn_id, actor, lane_id, projection) in [
            (
                "submitted",
                "human",
                "main",
                json!({"content": "original question",
                "_branchLanes": [{"laneId": "earlier-kept"}]}),
            ),
            (
                "target",
                "assistant",
                "main",
                json!({
                    "content": "partial answer", "thinking": "checkpoint thinking", "segments": [],
                    "toolRounds": [
                        {"roundNum": 1, "toolCallId": "kept", "status": "done"},
                        {"roundNum": 2, "toolCallId": "rewound", "status": "done"}
                    ], "_branchLanes": [{"laneId": "target-kept"}]
                }),
            ),
            (
                "tail-input",
                "human",
                "main",
                json!({"content": "later question"}),
            ),
            (
                "tail-output",
                "assistant",
                "main",
                json!({"content": "later answer",
                "_branchLanes": [{"laneId": "tail-branch"}]}),
            ),
            (
                "branch-output",
                "assistant",
                "tail-branch",
                json!({"content": "branch answer",
                "_branchLanes": [{"laneId": "nested"}]}),
            ),
            (
                "nested-output",
                "assistant",
                "nested",
                json!({"content": "nested answer"}),
            ),
            (
                "earlier-output",
                "assistant",
                "earlier-kept",
                json!({"content": "keep earlier"}),
            ),
            (
                "target-output",
                "assistant",
                "target-kept",
                json!({"content": "keep target"}),
            ),
            (
                "unrelated",
                "assistant",
                "independent",
                json!({"content": "keep unrelated"}),
            ),
        ] {
            // Pending human tail has no Attempt: exercise turn liveness alone.
            let status = if (turn_id == "tail-input" && live_kind == Some("main_turn"))
                || (turn_id == "nested-output" && live_kind == Some("nested_attempt"))
            {
                "pending"
            } else {
                "completed"
            };
            append_resume_tail_turn(
                database,
                transaction,
                turn_id,
                actor,
                lane_id,
                projection,
                status,
            );
        }
    }

    fn resume_tail_request(operation: &str) -> AttemptCreateRequest {
        AttemptCreateRequest {
            conversation_id: "resume-tail".to_owned(),
            turn_id: "target".to_owned(),
            attempt_id: "resumed-attempt".to_owned(),
            command_id: "resume-command".to_owned(),
            operation: operation.to_owned(),
            dispatch_mode: "conversation_executor".to_owned(),
            expected_projection_revision: 1,
            resume_anchor: None,
            config: if operation == "answer_guidance" {
                json!({"_humanGuidanceAnswer": {
                    "guidanceId": "fixture-guidance", "response": "Continue please"
                }})
            } else {
                json!({})
            },
            input_update: None,
            expected_input_projection_revision: None,
            target_actor: (operation == "regenerate").then(|| "assistant".to_owned()),
            target_kind: (operation == "regenerate").then(|| "reply".to_owned()),
            created_at_ms: 200,
            committed_at_ms: 200,
        }
    }

    #[test]
    fn attempt_discards_later_lane_and_rooted_branches_and_replays() {
        for operation in [
            "continue",
            "checkpoint_resume",
            "answer_guidance",
            "regenerate",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            seed_resume_tail(&database, &mut seed, None);
            database.commit(seed).unwrap();
            let request = resume_tail_request(operation);
            let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let result: Value =
                serde_json::from_slice(&attempt_create(&database, &mut write, &request).unwrap())
                    .unwrap();
            let discarded = json!([
                "branch-output",
                "nested-output",
                "tail-input",
                "tail-output"
            ]);
            assert_eq!(result["deletedTurnIds"], discarded, "{operation}");
            assert_eq!(result["idempotentReplay"], false);
            let projection = &result["turn"]["projection"];
            if matches!(operation, "continue" | "answer_guidance") {
                assert_eq!(projection["content"], "partial answer");
                assert_eq!(projection["thinking"], "checkpoint thinking");
                assert_eq!(projection["toolRounds"].as_array().unwrap().len(), 2);
            } else if operation == "checkpoint_resume" {
                assert_eq!(projection["content"], "");
                assert_eq!(projection["thinking"], "");
                assert_eq!(projection["toolRounds"].as_array().unwrap().len(), 1);
                assert_eq!(projection["toolRounds"][0]["toolCallId"], "kept");
                assert_eq!(projection["rolledBack"][0]["content"], "partial answer");
            } else {
                assert_eq!(projection["content"], "");
                assert_eq!(projection["toolRounds"], json!([]));
            }
            if operation != "regenerate" {
                assert_eq!(
                    projection["_branchLanes"],
                    json!([{"laneId": "target-kept"}])
                );
            }
            database.commit(write).unwrap();
            let mut inspect = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            for turn_id in [
                "tail-input",
                "tail-output",
                "branch-output",
                "nested-output",
            ] {
                assert!(get(&database, &mut inspect, "resume-tail", turn_id)
                    .unwrap()
                    .is_none());
                if turn_id != "tail-input" {
                    assert!(
                        attempt_get(&database, &mut inspect, &format!("seed-{turn_id}"))
                            .unwrap()
                            .is_none()
                    );
                }
            }
            for turn_id in [
                "submitted",
                "target",
                "earlier-output",
                "target-output",
                "unrelated",
            ] {
                assert!(get(&database, &mut inspect, "resume-tail", turn_id)
                    .unwrap()
                    .is_some());
            }
            drop(inspect);
            let mut append = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            append_resume_tail_turn(
                &database,
                &mut append,
                "after-resume",
                "human",
                "main",
                json!({"content": "new tail"}),
                "completed",
            );
            database.commit(append).unwrap();
            let mut replay = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let mut stale_request = request.clone();
            stale_request.expected_projection_revision = 0;
            stale_request.input_update = Some(
                if operation == "regenerate" {
                    json!({"content": "must not edit"})
                } else {
                    json!({"contextSnapshot": {"snapshot": {"model": "must not edit"}}})
                }
                .as_object()
                .unwrap()
                .clone(),
            );
            let repeated: Value = serde_json::from_slice(
                &attempt_create(&database, &mut replay, &stale_request).unwrap(),
            )
            .unwrap();
            assert_eq!(repeated["deletedTurnIds"], discarded);
            assert_eq!(repeated["attempt"], result["attempt"]);
            assert_eq!(repeated["idempotentReplay"], true);
            assert!(!replay.has_business_mutation());
            assert!(get(&database, &mut replay, "resume-tail", "after-resume")
                .unwrap()
                .is_some());
            let submitted: Value = serde_json::from_slice(
                &get(&database, &mut replay, "resume-tail", "submitted")
                    .unwrap()
                    .unwrap(),
            )
            .unwrap();
            assert_eq!(submitted["projection"]["content"], "original question");
        }
    }

    #[test]
    fn attempt_live_tail_conflict_precedes_all_native_mutations() {
        for operation in [
            "continue",
            "checkpoint_resume",
            "answer_guidance",
            "regenerate",
        ] {
            for live_kind in ["main_turn", "nested_attempt"] {
                let directory = tempfile::tempdir().unwrap();
                let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
                let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
                seed_resume_tail(&database, &mut seed, Some(live_kind));
                database.commit(seed).unwrap();
                let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
                let mut request = resume_tail_request(operation);
                request.expected_input_projection_revision = Some(1);
                request.input_update = Some(
                    if operation == "regenerate" {
                        json!({"content": "edited question"})
                    } else {
                        json!({"contextSnapshot": {"snapshot": {"model": "new model"}}})
                    }
                    .as_object()
                    .unwrap()
                    .clone(),
                );
                let error = attempt_create(&database, &mut write, &request).unwrap_err();
                assert_eq!(
                    error.kind(),
                    io::ErrorKind::WouldBlock,
                    "{operation}/{live_kind}"
                );
                assert!(!write.has_business_mutation(), "{operation}/{live_kind}");
                let submitted: Value = serde_json::from_slice(
                    &get(&database, &mut write, "resume-tail", "submitted")
                        .unwrap()
                        .unwrap(),
                )
                .unwrap();
                assert_eq!(submitted["projectionRevision"], 1);
                assert_eq!(submitted["projection"]["content"], "original question");
                assert!(attempt_get(&database, &mut write, "resumed-attempt")
                    .unwrap()
                    .is_none());
            }
        }
    }

    #[test]
    fn attempt_tail_discard_preserves_stale_and_owner_fences() {
        for operation in [
            "continue",
            "checkpoint_resume",
            "answer_guidance",
            "regenerate",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            seed_resume_tail(&database, &mut seed, None);
            database.commit(seed).unwrap();
            let request = resume_tail_request(operation);
            let mut stale = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let mut stale_request = request.clone();
            stale_request.expected_projection_revision = 0;
            let error = attempt_create(&database, &mut stale, &stale_request).unwrap_err();
            assert_eq!(
                conflict_kind(&error),
                Some(TurnConflictKind::ProjectionStale)
            );
            assert!(!stale.has_business_mutation());
            stale_request.expected_projection_revision = 1;
            stale_request.resume_anchor = Some(json!({"stale": true}).as_object().unwrap().clone());
            assert_eq!(
                attempt_create(&database, &mut stale, &stale_request)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::WouldBlock
            );
            assert!(!stale.has_business_mutation());
            drop(stale);
            for (tenant_id, user_id) in [(7, 12), (8, 11)] {
                let mut other = database
                    .begin_with_identity_claim_scopes(tenant_id, user_id)
                    .unwrap();
                assert_eq!(
                    attempt_create(&database, &mut other, &request)
                        .unwrap_err()
                        .kind(),
                    io::ErrorKind::NotFound
                );
                assert!(!other.has_business_mutation());
            }
            let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            attempt_create(&database, &mut write, &request).unwrap();
            database.commit(write).unwrap();
            let mut other = database.begin_with_identity_claim_scopes(7, 12).unwrap();
            assert_eq!(
                attempt_create(&database, &mut other, &request)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::NotFound
            );
            assert!(!other.has_business_mutation());
        }
    }

    #[test]
    fn attempt_tail_discard_is_confined_to_target_branch_lane() {
        for operation in [
            "continue",
            "checkpoint_resume",
            "answer_guidance",
            "regenerate",
        ] {
            let directory = tempfile::tempdir().unwrap();
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            for (turn_id, actor, lane_id, projection) in [
                (
                    "unaffected-main",
                    "human",
                    "main",
                    json!({"content": "keep main"}),
                ),
                (
                    "submitted",
                    "human",
                    "resumed-branch",
                    json!({"content": "question"}),
                ),
                (
                    "target",
                    "assistant",
                    "resumed-branch",
                    json!({
                        "content": "partial", "toolRounds": [{"toolCallId": "kept", "status": "done"}]
                    }),
                ),
                (
                    "branch-tail",
                    "assistant",
                    "resumed-branch",
                    json!({"content": "discard",
                    "_branchLanes": [{"laneId": "nested"}]}),
                ),
                (
                    "nested-tail",
                    "assistant",
                    "nested",
                    json!({"content": "discard nested"}),
                ),
            ] {
                append_resume_tail_turn(
                    &database,
                    &mut seed,
                    turn_id,
                    actor,
                    lane_id,
                    projection,
                    "completed",
                );
            }
            database.commit(seed).unwrap();
            let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            let result: Value = serde_json::from_slice(
                &attempt_create(&database, &mut write, &resume_tail_request(operation)).unwrap(),
            )
            .unwrap();
            assert_eq!(
                result["deletedTurnIds"],
                json!(["branch-tail", "nested-tail"])
            );
            database.commit(write).unwrap();
            let mut inspect = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            for turn_id in ["unaffected-main", "submitted", "target"] {
                assert!(get(&database, &mut inspect, "resume-tail", turn_id)
                    .unwrap()
                    .is_some());
            }
            for turn_id in ["branch-tail", "nested-tail"] {
                assert!(get(&database, &mut inspect, "resume-tail", turn_id)
                    .unwrap()
                    .is_none());
            }
        }
    }
