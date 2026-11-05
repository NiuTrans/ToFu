// turn.event.record_batch: K ordinary TurnEventRecord steps in one transaction.

const TURN_EVENT_BATCH_TASK: &str = "batch-task";

#[test]
fn turn_terminal_retry_replays_the_committed_result_after_lost_ack_and_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let (attempt_id, turn_id, revision) = turn_event_batch_fixture(&mut database, "terminal-retry");
    let mut payload = turn_event_batch_entries(&attempt_id, revision, 0).remove(0);
    payload["terminal"] = json!(true);
    payload["status"] = json!("completed");
    payload["task_event"]["event"] = json!({"type":"done", "finishReason":"stop"});
    let original = request("turn.event.record", 11, payload.clone(), Some("terminal-retry-command"));
    let committed = execute(&mut database, &original, 3_000).unwrap();
    assert_eq!(committed["applied"], true);
    let mut transaction = database.begin(7, 11).unwrap();
    let key = crate::receipt::command_receipt_entity_key(7, 11, "terminal-retry-command").unwrap();
    let stored = database.entity_get(&mut transaction, &key).unwrap().unwrap();
    println!("TERMINAL_RECEIPT response_bytes={} entity_value_bytes={}",
        serde_json::to_vec(&committed).unwrap().len(), stored.len());
    drop(transaction);
    let state = turn_event_batch_state(&mut database, "terminal-retry", &turn_id, &attempt_id);
    let wal = database.wal_len().unwrap();
    assert_eq!(execute(&mut database, &original, 4_000).unwrap(), committed,
        "a lost terminal ACK must replay success instead of rejecting a now-settled attempt");
    assert_eq!(database.wal_len().unwrap(), wal);
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(execute(&mut database, &original, 5_000).unwrap(), committed);
    assert_eq!(turn_event_batch_state(&mut database, "terminal-retry", &turn_id, &attempt_id), state);
    payload["task_event"]["event"]["finishReason"] = json!("different");
    assert!(execute(&mut database, &request("turn.event.record", 11, payload,
        Some("terminal-retry-command")), 6_000).is_err());
    let mut foreign = original.clone();
    foreign.owner_id = 12;
    assert_ne!(execute(&mut database, &foreign, 7_000).ok().as_ref(), Some(&committed));
}

fn turn_event_batch_fixture(database: &mut AuthorityDatabase, conversation: &str) -> (String, String, u64) {
    let turn_id = format!("{conversation}-turn");
    execute(database, &request("conversation.create", 11, json!({
        "conv_id": conversation, "user_id": 11, "title": "Batch", "settings": {}
    }), Some(&format!("{conversation}-create"))), 1_000).unwrap();
    execute(database, &request("turn.append_settled", 11, json!({
        "conversation_id": conversation, "user_id": 11, "command_id": format!("{conversation}-seed"),
        "actor": "assistant", "kind": "reply", "status": "completed",
        "projection": {"content": "", "thinking": "", "segments": []},
        "settlement": {"outcome": "completed", "cause": "seed", "resumeOptions": []},
        "turn_id": turn_id, "created_at": 1_100
    }), Some(&format!("{conversation}-seed-command"))), 1_100).unwrap();
    let created = execute(database, &request("turn.attempt.create", 11, json!({
        "conversation_id": conversation, "turn_id": turn_id, "user_id": 11,
        "command_id": format!("{conversation}-attempt"), "operation": "regenerate",
        "target_actor": "assistant", "target_kind": "reply",
        "expected_projection_revision": 1, "config": {}
    }), Some(&format!("{conversation}-attempt-command"))), 1_150).unwrap();
    let attempt_id = created["attempt"]["attemptId"].as_str().unwrap().to_owned();
    execute(database, &request("turn.attempt.bind", 11, json!({
        "attempt_id": attempt_id, "task_id": TURN_EVENT_BATCH_TASK, "user_id": 11
    }), Some(&format!("{conversation}-bind"))), 1_200).unwrap();
    let started = execute(database, &request("turn.event.record", 11, json!({
        "attempt_id": attempt_id, "user_id": 11, "task_id": TURN_EVENT_BATCH_TASK,
        "slim": true, "content": "", "thinking": "",
        "event_payload": {"phase": "starting", "updateKind": "phase"}, "now": 1_300
    }), Some(&format!("{conversation}-start"))), 1_300).unwrap();
    assert_eq!(started["applied"], true);
    (attempt_id, turn_id, started["projection_revision"].as_u64().unwrap())
}

/// Text, top-level structural and tool-round entries chained from `base`.
fn turn_event_batch_entries(attempt_id: &str, base: u64, first_sequence: u64) -> Vec<Value> {
    let operations = [
        json!([{"op": "set", "path": ["content"], "value": "reading"}]),
        json!([{"op": "set", "path": ["fallbackModel"], "value": "model-b"}]),
        json!([{"op": "set", "path": ["toolRounds"], "value": [
            {"roundNum": 1, "toolCallId": "call-1", "toolName": "read_file", "status": "running"}]}]),
        json!([{"op": "set", "path": ["content"], "value": "reading done"}]),
    ];
    operations.iter().enumerate().map(|(offset, operations)| {
        let offset = offset as u64;
        json!({
            "attempt_id": attempt_id, "user_id": 11, "task_id": TURN_EVENT_BATCH_TASK,
            "terminal": false, "status": "running", "settlement": {}, "error": {},
            "event_type": "projection_updated",
            "event_payload": {"phase": "calling", "updateKind": "tool_start"},
            "projection_patch": {"version": 1, "baseRevision": base + offset,
                "targetRevision": base + offset + 1, "operations": operations},
            "task_event": {"task_id": TURN_EVENT_BATCH_TASK, "sequence": first_sequence + offset,
                "event": {"type": "tool_start", "roundNum": offset}},
            "now": 2_000 + offset
        })
    }).collect()
}

fn turn_event_batch_state(database: &mut AuthorityDatabase, conversation: &str, turn_id: &str, attempt_id: &str) -> (Value, Value, Value, Value) {
    let turn = execute(database, &request("turn.get", 11, json!({
        "conversation_id": conversation, "turn_id": turn_id, "user_id": 11
    }), None), 9_000).unwrap();
    let attempt_events = execute(database, &request("turn.events.list", 11, json!({
        "attempt_id": attempt_id, "user_id": 11, "projection_mode": "patch"
    }), None), 9_000).unwrap();
    let rows = execute(database, &request("event.list", 11, json!({
        "task_id": TURN_EVENT_BATCH_TASK, "after_sequence": -1, "limit": 1000
    }), None), 9_000).unwrap();
    let changes = execute(database, &request("turn.sync.changes", 11, json!({
        "conversation_id": conversation, "user_id": 11, "after": 0
    }), None), 9_000).unwrap();
    (turn, attempt_events, rows, changes)
}

fn turn_event_batch_comparable(state: &(Value, Value, Value, Value)) -> Value {
    let (turn, attempt_events, rows, changes) = state;
    json!({
        "revision": turn["projectionRevision"],
        "projection": turn["projection"],
        "patches": attempt_events.as_array().unwrap().iter()
            .map(|event| event["payload"]["projectionPatch"].clone()).collect::<Vec<_>>(),
        "rows": rows.as_array().unwrap().iter()
            .map(|row| json!([row["sequence"], row["event"]])).collect::<Vec<_>>(),
        "changes": changes["events"].as_array().unwrap().iter()
            .map(|change| json!([change["type"], change["syncSeq"]])).collect::<Vec<_>>(),
    })
}

#[test]
fn turn_event_record_batch_equals_sequential_records_in_one_transaction() {
    let sequential_directory = tempfile::tempdir().unwrap();
    let batched_directory = tempfile::tempdir().unwrap();
    let mut sequential = AuthorityDatabase::initialize(sequential_directory.path()).unwrap();
    let mut batched = AuthorityDatabase::initialize(batched_directory.path()).unwrap();
    let (attempt_id, turn_id, base) = turn_event_batch_fixture(&mut sequential, "batch-conv");
    let (batched_attempt, batched_turn, batched_base) = turn_event_batch_fixture(&mut batched, "batch-conv");
    assert_eq!((&batched_turn, batched_base), (&turn_id, base));
    for (index, entry) in turn_event_batch_entries(&attempt_id, base, 0).into_iter().enumerate() {
        let result = execute(&mut sequential, &request("turn.event.record", 11, entry,
            Some(&format!("sequential-{index}"))), 2_000 + index as u64).unwrap();
        assert_eq!(result["applied"], true);
    }
    let entries = turn_event_batch_entries(&batched_attempt, batched_base, 0);
    let before_commits = batched.writer_engine().unwrap().next_sequence().unwrap();
    let result = execute(&mut batched, &request("turn.event.record_batch", 11,
        json!({"user_id": 11, "entries": entries}), Some("batched")), 2_000).unwrap();
    assert_eq!(batched.writer_engine().unwrap().next_sequence().unwrap(), before_commits + 1,
        "the whole batch must commit as one transaction");
    let results = result.as_array().unwrap();
    assert_eq!(results.len(), 4);
    for (offset, item) in results.iter().enumerate() {
        assert_eq!(item["applied"], true);
        assert_eq!(item["status"], "running");
        assert_eq!(item["projection_revision"], base + offset as u64 + 1);
        assert_eq!(item["task_event_count"], 1);
        assert!(item["task_event"].is_object());
    }
    let sequential_state = turn_event_batch_state(&mut sequential, "batch-conv", &turn_id, &attempt_id);
    let batched_state = turn_event_batch_state(&mut batched, "batch-conv", &batched_turn, &batched_attempt);
    assert_eq!(batched_state.0["projectionRevision"], base + 4);
    assert_eq!(turn_event_batch_comparable(&batched_state), turn_event_batch_comparable(&sequential_state));
    drop(batched);
    let mut reopened = AuthorityDatabase::open(batched_directory.path()).unwrap();
    let reopened_state = turn_event_batch_state(&mut reopened, "batch-conv", &batched_turn, &batched_attempt);
    assert_eq!(turn_event_batch_comparable(&reopened_state), turn_event_batch_comparable(&sequential_state));
}

#[test]
fn turn_event_record_batch_rolls_back_every_entry_when_one_step_fails() {
    for invalid in ["stale_base", "conflicting_row", "late_entry_stale_attempt"] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let (attempt_id, turn_id, base) = turn_event_batch_fixture(&mut database, "rollback-conv");
        let mut entries = turn_event_batch_entries(&attempt_id, base, 0);
        match invalid {
            "stale_base" => {
                entries[2]["projection_patch"]["baseRevision"] = json!(base + 1);
                entries[2]["projection_patch"]["targetRevision"] = json!(base + 2);
            }
            "conflicting_row" => {
                execute(&mut database, &request("event.append", 11, json!({
                    "task_id": TURN_EVENT_BATCH_TASK, "sequence": 3,
                    "event": {"type": "delta", "content": "different"}
                }), Some("conflicting-row")), 1_900).unwrap();
            }
            _ => {}
        }
        let before = turn_event_batch_state(&mut database, "rollback-conv", &turn_id, &attempt_id);
        let mut ir = compile_admitted_transaction(admit_request(&request("turn.event.record_batch", 11,
            json!({"user_id": 11, "entries": entries}), Some("rollback-batch")),
            NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION },
            AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 2_000).unwrap(), 2_000).unwrap();
        if invalid == "late_entry_stale_attempt" {
            // Only a hand-built IR can diverge after an applied step; the
            // executor still refuses to commit a mixed outcome.
            let TransactionIrStep::TurnEventRecord { request, .. } = &mut ir.steps[3] else { panic!() };
            request.attempt_id = "missing-attempt".to_owned();
        }
        let error = crate::transaction_ir::execute_transaction_ir(&mut database, &ir).unwrap_err();
        let expected = match invalid {
            "stale_base" => SemanticExecutionError::TurnProjectionStale,
            _ => SemanticExecutionError::Conflict,
        };
        assert_eq!(map_storage_error(error), expected, "{invalid}");
        let after = turn_event_batch_state(&mut database, "rollback-conv", &turn_id, &attempt_id);
        assert_eq!(turn_event_batch_comparable(&after), turn_event_batch_comparable(&before), "{invalid}");
    }
}

#[test]
fn turn_event_record_batch_compiles_ordinary_steps_and_an_array_result() {
    let entries = turn_event_batch_entries("attempt-a", 5, 10);
    let ir = compile_admitted_transaction(admit_request(&request("turn.event.record_batch", 11,
        json!({"user_id": 11, "entries": entries}), Some("compile-batch")),
        NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION },
        AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 2_000).unwrap(), 2_000).unwrap();
    assert_eq!(ir.steps.len(), 4);
    for (index, step) in ir.steps.iter().enumerate() {
        let TransactionIrStep::TurnEventRecord { request, slot } = step else {
            panic!("batch entry {index} did not compile to an ordinary TurnEventRecord");
        };
        assert_eq!(*slot, index);
        assert_eq!(request.task_event.as_ref().unwrap().application_sequence, 10 + index as u64);
        assert_eq!(request.now_ms, 2_000 + index as u64);
    }
    assert!(matches!(&ir.result, TransactionIrResult::SlotArray { slots } if *slots == vec![0, 1, 2, 3]));
    ir.validate().unwrap();
    let mut unread = ir.clone();
    unread.result = TransactionIrResult::SlotArray { slots: vec![0, 4] };
    assert!(unread.validate().is_err());
}

#[test]
fn turn_event_record_batch_refuses_ineligible_or_unordered_entries_before_io() {
    let compile = |entries: Vec<Value>| {
        let request = request("turn.event.record_batch", 11,
            json!({"user_id": 11, "entries": entries}), Some("refused-batch"));
        let negotiated = NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION };
        let scope = AuthenticatedScope { owner_id: 11, tenant_id: Some(7) };
        let admitted = if request.payload.len() > crate::generated_storage_v2::MAX_PAYLOAD_BYTES {
            crate::semantic::admit_streamed_request(&request, negotiated, scope, 2_000, usize::MAX)
        } else {
            admit_request(&request, negotiated, scope, 2_000)
        }.unwrap();
        compile_admitted_transaction(admitted, 2_000).map(|_| ())
    };
    assert!(compile(turn_event_batch_entries("attempt-a", 5, 0)).is_ok());
    assert_eq!(compile(Vec::new()).unwrap_err().code(), "database_protocol_error");
    let mutations: Vec<(&str, Box<dyn Fn(&mut Vec<Value>)>)> = vec![
        ("terminal", Box::new(|entries| { entries[1]["terminal"] = json!(true); entries[1]["status"] = json!("completed"); })),
        ("interaction", Box::new(|entries| entries[1]["event_type"] = json!("interaction_request"))),
        ("background_ack", Box::new(|entries| entries[1]["background_command_ack"] = json!({"block_id": "b", "deliveries": []}))),
        ("effect_update", Box::new(|entries| entries[1]["event_payload"]["updateKind"] = json!("tool_effect"))),
        ("effect_row", Box::new(|entries| entries[1]["task_event"]["event"]["type"] = json!("tool_effect"))),
        ("missing_carrier", Box::new(|entries| { entries[1].as_object_mut().unwrap().remove("task_event"); })),
        ("foreign_row", Box::new(|entries| entries[1]["task_event"]["task_id"] = json!("other-task"))),
        ("foreign_task", Box::new(|entries| entries[1]["task_id"] = json!("other-task"))),
        ("other_attempt", Box::new(|entries| entries[1]["attempt_id"] = json!("attempt-b"))),
        ("other_flow_root", Box::new(|entries| entries[1]["flow_root_attempt_id"] = json!("root-b"))),
        ("other_owner", Box::new(|entries| entries[1]["user_id"] = json!(12))),
        ("repeated_row", Box::new(|entries| entries[1]["task_event"]["sequence"] = json!(0))),
        ("descending_row", Box::new(|entries| entries[2]["task_event"]["sequence"] = json!(0))),
        ("entry_not_object", Box::new(|entries| entries[1] = json!("entry"))),
        ("count_limit", Box::new(|entries| {
            let last = &mut entries[3]["task_event"];
            last["event"] = json!({"type": "delta", "content": "x"});
            last["preceding_events"] = json!((0..499).map(|index| json!({
                "task_id": TURN_EVENT_BATCH_TASK, "sequence": 3 + index,
                "event": {"type": "delta", "content": "x"}})).collect::<Vec<_>>());
            last["sequence"] = json!(3 + 499);
        })),
        ("byte_limit", Box::new(|entries| {
            let text = "x".repeat(16 * 1024 * 1024);
            for (index, entry) in entries.iter_mut().enumerate() {
                entry["task_event"]["event"] = json!({"type": "tool_start", "roundNum": index, "detail": text});
            }
        })),
    ];
    for (name, mutate) in mutations {
        let mut entries = turn_event_batch_entries("attempt-a", 5, 0);
        mutate(&mut entries);
        let error = compile(entries).unwrap_err();
        assert!(matches!(error.code(), "database_protocol_error" | "authority_scope_mismatch"), "{name}: {error:?}");
    }
    // The count fence counts rows: exactly 500 rows compile.
    let mut boundary = turn_event_batch_entries("attempt-a", 5, 0);
    let last = &mut boundary[3]["task_event"];
    last["event"] = json!({"type": "delta", "content": "x"});
    last["preceding_events"] = json!((0..496).map(|index| json!({
        "task_id": TURN_EVENT_BATCH_TASK, "sequence": 3 + index,
        "event": {"type": "delta", "content": "x"}})).collect::<Vec<_>>());
    last["sequence"] = json!(3 + 496);
    assert!(compile(boundary).is_ok());
}

#[test]
fn turn_event_record_batch_reports_a_stale_attempt_for_every_entry_without_writes() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let (attempt_id, turn_id, base) = turn_event_batch_fixture(&mut database, "stale-conv");
    let settled = execute(&mut database, &request("turn.event.record", 11, json!({
        "attempt_id": attempt_id, "user_id": 11, "terminal": true, "status": "cancelled",
        "settlement": {"outcome": "cancelled", "cause": "test", "resumeOptions": []}, "now": 1_800
    }), Some("stale-settle")), 1_800).unwrap();
    assert_eq!(settled["applied"], true);
    let before = turn_event_batch_state(&mut database, "stale-conv", &turn_id, &attempt_id);
    let result = execute(&mut database, &request("turn.event.record_batch", 11, json!({
        "user_id": 11, "entries": turn_event_batch_entries(&attempt_id, base + 1, 0)
    }), Some("stale-batch")), 2_000).unwrap();
    assert_eq!(result, json!([{"applied": false}, {"applied": false}, {"applied": false}, {"applied": false}]));
    let after = turn_event_batch_state(&mut database, "stale-conv", &turn_id, &attempt_id);
    assert_eq!(turn_event_batch_comparable(&after), turn_event_batch_comparable(&before));
}

/// Deterministic bytes no block codec can shrink.
fn turn_event_batch_noise(state: &mut u64, bytes: usize) -> Vec<u8> {
    (0..bytes).map(|_| {
        *state ^= *state << 13;
        *state ^= *state >> 7;
        *state ^= *state << 17;
        (*state >> 24) as u8
    }).collect()
}

/// Deterministic text a block codec shrinks only to about three quarters.
fn turn_event_batch_noise_text(state: &mut u64, bytes: usize) -> String {
    const SYMBOLS: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    turn_event_batch_noise(state, bytes).into_iter()
        .map(|byte| char::from(SYMBOLS[usize::from(byte & 63)])).collect()
}

/// Distinct, poorly compressible tool-start entries chained from `base`.
fn turn_event_batch_bulky_entries(attempt_id: &str, base: u64, count: u64, detail_bytes: usize) -> Vec<Value> {
    let mut state = 0x9e37_79b9_7f4a_7c15_u64;
    (0..count).map(|offset| {
        let detail = turn_event_batch_noise_text(&mut state, detail_bytes);
        json!({
            "attempt_id": attempt_id, "user_id": 11, "task_id": TURN_EVENT_BATCH_TASK,
            "terminal": false, "status": "running", "settlement": {}, "error": {},
            "event_type": "projection_updated",
            "event_payload": {"phase": "calling", "updateKind": "tool_start"},
            "projection_patch": {"version": 1, "baseRevision": base + offset,
                "targetRevision": base + offset + 1, "operations": [
                    {"op": "set", "path": ["content"], "value": format!("step {offset}")}]},
            "task_event": {"task_id": TURN_EVENT_BATCH_TASK, "sequence": offset,
                "event": {"type": "tool_start", "roundNum": offset, "detail": detail}},
            "now": 2_000 + offset
        })
    }).collect()
}

#[test]
fn turn_event_record_batch_beyond_one_ordinary_commit_is_refused_and_its_halves_never_spill() {
    use crate::vfs::{DeterministicVfs, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
    let (attempt_id, turn_id, base) = turn_event_batch_fixture(&mut database, "split-conv");
    // 64 rows of 32 KiB exceed the composition capacity of one WAL-carried
    // commit even without the one largest row, before any projection or
    // index bytes are counted.
    let entries = turn_event_batch_bulky_entries(&attempt_id, base, 64, 32 * 1024);
    let before = turn_event_batch_state(&mut database, "split-conv", &turn_id, &attempt_id);
    let before_sequence = database.writer_engine().unwrap().next_sequence().unwrap();
    let batch = |entries: &[Value], name: &str| request("turn.event.record_batch", 11,
        json!({"user_id": 11, "entries": entries}), Some(name));

    let error = execute(&mut database, &batch(&entries, "split-whole"), 2_000).unwrap_err();
    assert_eq!(error.code(), "database_resource_exhausted", "{error:?}");
    // The same refusal reaches the parallel preparation lane before commit.
    let ir = compile_admitted_transaction(admit_request(&batch(&entries, "split-prepared"),
        NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION },
        AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 2_000).unwrap(), 2_000).unwrap();
    let mut replica = database.build_preparation_replica().unwrap();
    let prepared = crate::transaction_ir::prepare_transaction_ir_with_deadline(
        &mut replica, &ir, std::time::Duration::from_secs(600));
    drop(replica);
    let Err(prepared_error) = prepared else { panic!("prepared lane admitted an oversized batch") };
    assert_eq!(map_storage_error(prepared_error).code(), "database_resource_exhausted");
    assert_eq!(database.writer_engine().unwrap().next_sequence().unwrap(), before_sequence);
    let refused = turn_event_batch_state(&mut database, "split-conv", &turn_id, &attempt_id);
    assert_eq!(turn_event_batch_comparable(&refused), turn_event_batch_comparable(&before));

    // Halving until each part fits commits every entry, each part as one
    // ordinary commit whose new blocks ride in its WAL record.
    let loose_syncs = |vfs: &DeterministicVfs| vfs.trace().unwrap().iter()
        .filter(|operation| **operation == Operation::SyncAll).count();
    let syncs_before = loose_syncs(&vfs);
    let (mut first, mut size, mut commits) = (0, entries.len(), 0);
    while first < entries.len() {
        let part = &entries[first..(first + size).min(entries.len())];
        match execute(&mut database, &batch(part, &format!("split-{first}-{size}")), 2_000) {
            Ok(result) => {
                let results = result.as_array().unwrap();
                assert_eq!(results.len(), part.len());
                assert!(results.iter().all(|item| item["applied"] == true));
                first += part.len();
                commits += 1;
            }
            Err(error) if error.code() == "database_resource_exhausted" && part.len() > 1 => size = part.len() / 2,
            Err(error) => panic!("unexpected refusal: {error:?}"),
        }
    }
    assert!(commits >= 2);
    assert_eq!(loose_syncs(&vfs), syncs_before, "a committed batch must stay WAL-carried");

    let sequential_directory = tempfile::tempdir().unwrap();
    let mut sequential = AuthorityDatabase::initialize(sequential_directory.path()).unwrap();
    let (sequential_attempt, sequential_turn, sequential_base) = turn_event_batch_fixture(&mut sequential, "split-conv");
    assert_eq!(sequential_base, base);
    for (index, entry) in turn_event_batch_bulky_entries(&sequential_attempt, base, 64, 32 * 1024).into_iter().enumerate() {
        assert_eq!(execute(&mut sequential, &request("turn.event.record", 11, entry,
            Some(&format!("split-sequential-{index}"))), 2_000).unwrap()["applied"], true);
    }
    let batched = turn_event_batch_state(&mut database, "split-conv", &turn_id, &attempt_id);
    assert_eq!(batched.0["projectionRevision"], base + 64);
    assert_eq!(turn_event_batch_comparable(&batched),
        turn_event_batch_comparable(&turn_event_batch_state(&mut sequential, "split-conv", &sequential_turn, &sequential_attempt)));

    // One entry is an ordinary record: its size never refuses it, whichever
    // commit path the engine then selects.
    let directory = tempfile::tempdir().unwrap();
    let mut single = AuthorityDatabase::initialize(directory.path()).unwrap();
    let (single_attempt, _, single_base) = turn_event_batch_fixture(&mut single, "single-conv");
    let oversized = turn_event_batch_bulky_entries(&single_attempt, single_base, 1, 2560 * 1024);
    let result = execute(&mut single, &batch(&oversized, "single-oversized"), 2_000).unwrap();
    assert_eq!(result[0]["applied"], true);
}

/// `turn_event_batch_entries` from `base` whose entries at `large_at` each
/// carry more than one ordinary commit by themselves: 2 MiB of high-entropy
/// tool output folds the projection into a checkpoint blob beside an
/// attempt-event blob of the same patch, or a 2.5 MiB row takes its own blob.
fn turn_event_batch_with_large_entries(attempt_id: &str, base: u64, large: &str, large_at: &[usize]) -> Vec<Value> {
    let mut entries = turn_event_batch_entries(attempt_id, base, 0);
    let mut state = 0x5851_f42d_4c95_7f2d_u64;
    for &index in large_at {
        if large == "projection_checkpoint" {
            entries[index]["projection_patch"]["operations"] = json!([{"op": "set", "path": ["toolRounds"],
                "value": [{"roundNum": index, "toolCallId": format!("call-{index}"), "toolName": "read_file",
                    "status": "done", "toolContent": turn_event_batch_noise_text(&mut state, 2 * 1024 * 1024)}]}]);
        } else {
            entries[index]["task_event"]["event"]["detail"] =
                json!(turn_event_batch_noise_text(&mut state, 2560 * 1024));
        }
    }
    entries
}

/// Grows the projection by 3 MiB of high-entropy tool output, then streams
/// text: the next structural record retires that text tail and writes the
/// whole projection as one checkpoint, like a long-running Turn. Returns the
/// committed revision.
fn turn_event_batch_stream_after_a_large_projection(database: &mut AuthorityDatabase, attempt_id: &str, base: u64) -> u64 {
    let mut state = 0x7c15_9e37_79b9_7f4a_u64;
    let operations = [
        json!([{"op": "set", "path": ["toolRounds"], "value": [{"roundNum": 0, "toolCallId": "call-0",
            "toolName": "read_file", "status": "done",
            "toolContent": turn_event_batch_noise_text(&mut state, 3 * 1024 * 1024)}]}]),
        json!([{"op": "append_text", "path": ["content"], "value": "streamed text"}]),
    ];
    for (offset, operations) in (0_u64..).zip(operations) {
        let result = execute(database, &request("turn.event.record", 11, json!({
            "attempt_id": attempt_id, "user_id": 11, "task_id": TURN_EVENT_BATCH_TASK,
            "terminal": false, "status": "running", "settlement": {}, "error": {},
            "event_type": "projection_updated",
            "event_payload": {"phase": "calling", "updateKind": "tool_start"},
            "projection_patch": {"version": 1, "baseRevision": base + offset,
                "targetRevision": base + offset + 1, "operations": operations},
            "now": 1_500 + offset
        }), Some(&format!("stream-{offset}"))), 1_500).unwrap();
        assert_eq!(result["applied"], true);
    }
    base + 2
}

/// Records `entries` like `turn_lifecycle._record_task_events_locked`: a
/// refused composite bisects only that interval, in FIFO order. Returns the
/// per-entry results and the numbers of refused and committed batches.
fn turn_event_batch_record_like_the_producer(database: &mut AuthorityDatabase, entries: &[Value], name: &str)
    -> (Vec<Value>, usize, usize)
{
    let (mut results, mut refused, mut committed) = (Vec::new(), 0, 0);
    let mut intervals = vec![0..entries.len()];
    while let Some(interval) = intervals.pop() {
        let part = &entries[interval.clone()];
        let batch = request("turn.event.record_batch", 11, json!({"user_id": 11, "entries": part}),
            Some(&format!("{name}-{}-{}", interval.start, interval.end)));
        match execute(database, &batch, 2_000) {
            Ok(result) => {
                results.extend(result.as_array().unwrap().iter().cloned());
                committed += 1;
            }
            Err(error) if error.code() == "database_resource_exhausted" && part.len() > 1 => {
                refused += 1;
                let half = interval.start + part.len() / 2;
                intervals.push(half..interval.end);
                intervals.push(interval.start..half);
            }
            Err(error) => panic!("{name}: unexpected refusal: {error:?}"),
        }
    }
    (results, refused, committed)
}

#[test]
fn turn_event_record_batch_commits_one_entry_beyond_one_ordinary_commit_with_its_small_followers() {
    let capacity = crate::transaction::MAX_CARRIED_PAYLOAD_BYTES
        - crate::transaction::CARRIED_TRANSACTION_HEADROOM_BYTES;
    for large in ["text_tail_checkpoint", "projection_checkpoint", "task_event_row"] {
        let sequential_directory = tempfile::tempdir().unwrap();
        let batched_directory = tempfile::tempdir().unwrap();
        let mut sequential = AuthorityDatabase::initialize(sequential_directory.path()).unwrap();
        let mut batched = AuthorityDatabase::initialize(batched_directory.path()).unwrap();
        let (attempt_id, turn_id, mut base) = turn_event_batch_fixture(&mut sequential, "large-conv");
        let (batched_attempt, batched_turn, mut batched_base) = turn_event_batch_fixture(&mut batched, "large-conv");
        let entries_for = |attempt_id: &str, base: u64| match large {
            // The first structural entry after streamed text is the large one.
            "text_tail_checkpoint" => turn_event_batch_entries(attempt_id, base, 0),
            _ => turn_event_batch_with_large_entries(attempt_id, base, large, &[0]),
        };
        if large == "text_tail_checkpoint" {
            base = turn_event_batch_stream_after_a_large_projection(&mut sequential, &attempt_id, base);
            batched_base = turn_event_batch_stream_after_a_large_projection(&mut batched, &batched_attempt, batched_base);
        }
        assert_eq!((&batched_turn, batched_base), (&turn_id, base));

        // The preparation lane admits a composite beyond one ordinary commit.
        let ir = compile_admitted_transaction(admit_request(&request("turn.event.record_batch", 11,
            json!({"user_id": 11, "entries": entries_for(&attempt_id, base)}), Some("large-prepared")),
            NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION },
            AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 2_000).unwrap(), 2_000).unwrap();
        let mut replica = sequential.build_preparation_replica().unwrap();
        let prepared = crate::transaction_ir::prepare_transaction_ir_with_deadline(
            &mut replica, &ir, std::time::Duration::from_secs(600))
            .unwrap_or_else(|error| panic!("{large}: the preparation lane refused the composite: {error:?}"));
        let carried = replica.carried_payload_bytes(prepared.transaction.as_ref().unwrap()).unwrap();
        assert!(carried > capacity, "{large}: {carried} carried bytes fit one ordinary commit");
        drop(prepared);
        drop(replica);

        // Through the producer's bisection rule the whole run commits once;
        // the large entry is never re-executed in a refused interval.
        let entries = entries_for(&batched_attempt, batched_base);
        let before_commits = batched.writer_engine().unwrap().next_sequence().unwrap();
        let started = std::time::Instant::now();
        let (results, refused, committed) = turn_event_batch_record_like_the_producer(&mut batched, &entries, large);
        assert_eq!((refused, committed), (0, 1),
            "{large}: {refused} refused and {committed} committed batches in {:?}", started.elapsed());
        assert_eq!(batched.writer_engine().unwrap().next_sequence().unwrap(), before_commits + 1,
            "{large}: the whole batch must commit as one transaction");
        let sequential_results = entries_for(&attempt_id, base).into_iter().enumerate()
            .map(|(index, entry)| execute(&mut sequential, &request("turn.event.record", 11, entry,
                Some(&format!("{large}-sequential-{index}"))), 2_000).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(results, sequential_results, "{large}");
        assert!(results.iter().all(|item| item["applied"] == true && item["task_event_count"] == 1), "{large}");

        let sequential_state = turn_event_batch_state(&mut sequential, "large-conv", &turn_id, &attempt_id);
        let batched_state = turn_event_batch_state(&mut batched, "large-conv", &batched_turn, &batched_attempt);
        assert_eq!(batched_state.0["projectionRevision"], base + 4, "{large}");
        assert_eq!(turn_event_batch_comparable(&batched_state), turn_event_batch_comparable(&sequential_state),
            "{large}");
        drop(batched);
        let mut reopened = AuthorityDatabase::open(batched_directory.path()).unwrap();
        let reopened_state = turn_event_batch_state(&mut reopened, "large-conv", &batched_turn, &batched_attempt);
        assert_eq!(turn_event_batch_comparable(&reopened_state), turn_event_batch_comparable(&sequential_state),
            "{large}");
    }
}

#[test]
fn turn_event_record_batch_exempts_at_most_one_entry_beyond_one_ordinary_commit() {
    for case in ["two_checkpoints", "two_rows", "large_row_and_bulky_rows"] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let (attempt_id, turn_id, base) = turn_event_batch_fixture(&mut database, "exempt-conv");
        let entries = match case {
            "two_checkpoints" => turn_event_batch_with_large_entries(&attempt_id, base, "projection_checkpoint", &[0, 2]),
            "two_rows" => turn_event_batch_with_large_entries(&attempt_id, base, "task_event_row", &[0, 2]),
            _ => {
                // The exempt entry never widens the others' budget: 2 MiB of
                // small rows still exceed one ordinary commit beside it.
                let mut entries = turn_event_batch_bulky_entries(&attempt_id, base, 65, 32 * 1024);
                let mut state = 0x5851_f42d_4c95_7f2d_u64;
                entries[0]["task_event"]["event"]["detail"] = json!(turn_event_batch_noise_text(&mut state, 2560 * 1024));
                entries
            }
        };
        let before = turn_event_batch_state(&mut database, "exempt-conv", &turn_id, &attempt_id);
        let before_sequence = database.writer_engine().unwrap().next_sequence().unwrap();
        let error = execute(&mut database, &request("turn.event.record_batch", 11,
            json!({"user_id": 11, "entries": entries}), Some(case)), 2_000).unwrap_err();
        assert_eq!(error.code(), "database_resource_exhausted", "{case}: {error:?}");
        assert_eq!(database.writer_engine().unwrap().next_sequence().unwrap(), before_sequence, "{case}");
        let after = turn_event_batch_state(&mut database, "exempt-conv", &turn_id, &attempt_id);
        assert_eq!(turn_event_batch_comparable(&after), turn_event_batch_comparable(&before), "{case}");
    }
}

#[test]
fn turn_event_record_batch_failure_after_the_exempt_entry_writes_nothing() {
    for invalid in ["stale_base", "late_entry_stale_attempt"] {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let (attempt_id, turn_id, base) = turn_event_batch_fixture(&mut database, "exempt-rollback-conv");
        let mut entries = turn_event_batch_with_large_entries(&attempt_id, base, "projection_checkpoint", &[0]);
        if invalid == "stale_base" {
            entries[2]["projection_patch"]["baseRevision"] = json!(base + 1);
            entries[2]["projection_patch"]["targetRevision"] = json!(base + 2);
        }
        let before = turn_event_batch_state(&mut database, "exempt-rollback-conv", &turn_id, &attempt_id);
        let before_sequence = database.writer_engine().unwrap().next_sequence().unwrap();
        let mut ir = compile_admitted_transaction(admit_request(&request("turn.event.record_batch", 11,
            json!({"user_id": 11, "entries": entries}), Some("exempt-rollback")),
            NegotiatedProtocol { protocol_version: PROTOCOL_VERSION, schema_id: STORAGE_SCHEMA_VERSION },
            AuthenticatedScope { owner_id: 11, tenant_id: Some(7) }, 2_000).unwrap(), 2_000).unwrap();
        if invalid == "late_entry_stale_attempt" {
            let TransactionIrStep::TurnEventRecord { request, .. } = &mut ir.steps[3] else { panic!() };
            request.attempt_id = "missing-attempt".to_owned();
        }
        let error = crate::transaction_ir::execute_transaction_ir(&mut database, &ir).unwrap_err();
        let expected = match invalid {
            "stale_base" => SemanticExecutionError::TurnProjectionStale,
            _ => SemanticExecutionError::Conflict,
        };
        assert_eq!(map_storage_error(error), expected, "{invalid}");
        assert_eq!(database.writer_engine().unwrap().next_sequence().unwrap(), before_sequence, "{invalid}");
        let after = turn_event_batch_state(&mut database, "exempt-rollback-conv", &turn_id, &attempt_id);
        assert_eq!(turn_event_batch_comparable(&after), turn_event_batch_comparable(&before), "{invalid}");
    }
}

#[test]
fn turn_event_record_batch_commit_faults_beside_the_exempt_entry_recover_all_or_nothing() {
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};
    use std::path::Path;
    use std::sync::Arc;
    let fixture = || {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let identity = turn_event_batch_fixture(&mut database, "fault-conv");
        (vfs, database, identity)
    };
    let batch = |attempt_id: &str, base: u64| request("turn.event.record_batch", 11, json!({"user_id": 11,
        "entries": turn_event_batch_with_large_entries(attempt_id, base, "projection_checkpoint", &[0])}),
        Some("fault-batch"));
    let comparable = |database: &mut AuthorityDatabase, (attempt_id, turn_id, _): &(String, String, u64)|
        turn_event_batch_comparable(&turn_event_batch_state(database, "fault-conv", turn_id, attempt_id));

    let (vfs, mut baseline, identity) = fixture();
    let before = comparable(&mut baseline, &identity);
    vfs.arm_fault(None).unwrap();
    execute(&mut baseline, &batch(&identity.0, identity.2), 2_000).unwrap();
    let trace = vfs.trace().unwrap();
    let after = comparable(&mut baseline, &identity);
    drop(baseline);
    assert!(trace.iter().filter(|operation| **operation == Operation::SyncAll).count() > 1,
        "the composite spills its loose blocks before the WAL record");

    // The first and last barrier of each kind: spilled blocks, their
    // directories and the WAL record.
    let mut barriers = Vec::new();
    for kind in [Operation::SyncAll, Operation::SyncDirectory, Operation::SyncData] {
        let positions = trace.iter().enumerate().filter(|(_, operation)| **operation == kind)
            .map(|(index, _)| index).collect::<Vec<_>>();
        barriers.extend(positions.first().into_iter().chain(positions.last()).copied());
    }
    barriers.sort_unstable();
    barriers.dedup();
    let mut recovered_before = 0;
    for index in barriers {
        let operation = trace[index];
        let (vfs, mut database, identity) = fixture();
        vfs.arm_fault(Some(FaultRule {
            operation_number: index as u64 + 1,
            action: FaultAction::ErrorBefore(std::io::ErrorKind::Other),
        })).unwrap();
        let committed = execute(&mut database, &batch(&identity.0, identity.2), 2_000).is_ok();
        drop(database);
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let state = comparable(&mut recovered, &identity);
        assert!(state == after || (!committed && state == before),
            "fault at {operation:?} #{} recovered a partial batch", index + 1);
        recovered_before += usize::from(state == before);
    }
    assert!(recovered_before > 0, "some fault must precede the durable WAL record");
}

#[test]
fn one_ordinary_commit_counts_the_overlay_delta_and_new_carried_blocks() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let key = |index: usize| crate::entity::EntityKey::new(7, 11, "commit_shape_probe",
        format!("{index:08}").as_bytes()).unwrap();
    let ordinary = |database: &AuthorityDatabase, transaction: &crate::authority::AuthorityTransaction|
        database.commits_as_one_ordinary_delta(transaction, database.carried_payload_bytes(transaction).unwrap());
    let mut transaction = database.begin(7, 11).unwrap();
    database.entity_put(&mut transaction, key(0), vec![b'v'; 1024]).unwrap();
    assert!(ordinary(&database, &transaction));
    // 201 KiB of values leave no room for the overlay envelope reserve.
    for index in 1..=200 {
        database.entity_put(&mut transaction, key(index), vec![b'v'; 1024]).unwrap();
    }
    assert!(!ordinary(&database, &transaction));
    assert!(!database.commits_as_one_ordinary_delta(&transaction, 0),
        "no carried share exempts an entity delta beyond the overlay envelope");
    database.set_write_overlay_enabled(false);
    assert!(ordinary(&database, &transaction),
        "without the overlay a tree commit is the ordinary commit");
    database.set_write_overlay_enabled(true);
    drop(transaction);

    let mut state = 0x2545_f491_4f6c_dd1d_u64;
    let capacity = crate::transaction::MAX_CARRIED_PAYLOAD_BYTES
        - crate::transaction::CARRIED_TRANSACTION_HEADROOM_BYTES;
    let small = turn_event_batch_noise(&mut state, 64 * 1024);
    let large = turn_event_batch_noise(&mut state, capacity + 1);
    let mut transaction = database.begin(7, 11).unwrap();
    database.entity_put(&mut transaction, key(0), vec![b'v'; 16]).unwrap();
    database.stage_blob(&mut transaction, &mut small.as_slice(), 1 << 30).unwrap();
    assert!(ordinary(&database, &transaction));
    assert!(database.carried_payload_bytes(&transaction).unwrap() >= small.len());
    assert!(database.commits_as_one_ordinary_delta(&transaction, capacity));
    assert!(!database.commits_as_one_ordinary_delta(&transaction, capacity + 1));
    database.stage_blob(&mut transaction, &mut large.as_slice(), 1 << 30).unwrap();
    assert!(!ordinary(&database, &transaction));
    drop(transaction);

    // Private preparation bytes count exactly like pending writer blocks.
    let replica = database.build_preparation_replica().unwrap();
    let fresh = turn_event_batch_noise(&mut state, capacity + 1);
    let mut transaction = replica.begin(7, 11).unwrap();
    replica.entity_put(&mut transaction, key(1), vec![b'v'; 16]).unwrap();
    replica.stage_blob(&mut transaction, &mut small.as_slice(), 1 << 30).unwrap();
    assert!(ordinary(&replica, &transaction));
    replica.stage_blob(&mut transaction, &mut fresh.as_slice(), 1 << 30).unwrap();
    assert!(!ordinary(&replica, &transaction));
}
