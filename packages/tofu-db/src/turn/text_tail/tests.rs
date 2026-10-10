//! Text-tail durability, replay, snapshot and bounded-storage contracts.

use super::*;
use std::path::Path;
use std::sync::Arc;
use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

fn seed(database: &mut AuthorityDatabase) -> Map<String, Value> {
    let projection = json!({"content": "", "thinking": "",
        "toolRounds": [{"id": "stable-tool", "toolContent": "s".repeat(256 * 1024)}]});
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    append_settled(database, &mut transaction, &AppendSettledRequest {
        conversation_id: "text-conversation".to_owned(), actor: "assistant".to_owned(),
        status: "pending".to_owned(), projection_json: serde_json::to_vec(&projection).unwrap(),
        projection_timestamp: None, settlement_json: b"{}".to_vec(), lane_id: "main".to_owned(),
        command_id: "text-seed".to_owned(), kind: "reply".to_owned(), run_id: String::new(),
        turn_id: "text-turn".to_owned(), attempt_id: Some("text-attempt".to_owned()),
        created_at_ms: 1_000, committed_at_ms: 1_000,
        defaults: TurnDefaults { allow_create: true, title: "Text".to_owned(),
            settings_json: b"{}".to_vec(), created_at_ms: 1_000 },
    }).unwrap();
    database.commit(transaction).unwrap();
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    attempt_bind(database, &mut transaction, "text-attempt", "text-task", "", 1_100).unwrap();
    database.commit(transaction).unwrap();
    projection.as_object().unwrap().clone()
}

fn logical(database: &AuthorityDatabase, owner: u64) -> Value {
    let mut transaction = database.begin(7, owner).unwrap();
    serde_json::from_slice(&get(database, &mut transaction, "text-conversation", "text-turn")
        .unwrap().unwrap()).unwrap()
}

fn request(revision: u64, content: &str, thinking: &str) -> EventRecordRequest {
    EventRecordRequest {
        model_continuation: Value::Null, attempt_id: "text-attempt".to_owned(),
        task_id: "text-task".to_owned(), terminal: false, status: "running".to_owned(),
        slim: false, content: String::new(), thinking: String::new(), projection: Value::Null,
        projection_patch: Some(json!({"version": 1, "baseRevision": revision,
            "targetRevision": revision + 1, "operations": [
                {"op": "append_text", "path": ["content"], "value": content},
                {"op": "append_text", "path": ["thinking"], "value": thinking},
            ]}).as_object().unwrap().clone()),
        projection_segments_stable: false, settlement: Map::new(), error: Map::new(),
        event_type: "projection_updated".to_owned(), event_payload: Map::new(),
        background_command_ack: None, task_event: None, preceding_task_events: Vec::new(), flow_root_attempt_id: String::new(),
        now_ms: 2_000 + revision,
    }
}

fn append(database: &mut AuthorityDatabase, request: &EventRecordRequest) -> Value {
    let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    let result = event_record(database, &mut transaction, request).unwrap();
    let result: Value = serde_json::from_slice(&result).unwrap();
    if result["applied"] == true { database.commit(transaction).unwrap(); }
    result
}

fn physical(database: &AuthorityDatabase) -> Map<String, Value> {
    let mut transaction = database.begin(7, 11).unwrap();
    let key = turn_key(&transaction, "text-conversation", "text-turn").unwrap();
    let stored = database.entity_get(&mut transaction, &key).unwrap().unwrap();
    let (_, bytes) = crate::versioned_document::materialize_stored_document(
        database, 7, 11, &stored, DOCUMENT_IDENTITY).unwrap();
    decode_turn_value(&bytes).unwrap()
}

#[test]
fn empty_text_patch_keeps_payload_and_advances_the_replay_revision() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let revision = logical(&database, 11)["projectionRevision"].as_u64().unwrap();
    append(&mut database, &request(revision, "first😀", "thought"));
    let before = logical(&database, 11);
    let head = physical(&database)["_projectionHead"].clone();
    let mut heartbeat = request(revision + 1, "", "");
    heartbeat.projection_patch.as_mut().unwrap().insert("operations".to_owned(), json!([]));
    let result = append(&mut database, &heartbeat);
    assert_eq!(result["projection_revision"], revision + 2);
    let after = logical(&database, 11);
    assert_eq!(after["projection"], before["projection"]);
    let next_head = physical(&database)["_projectionHead"].clone();
    assert_eq!(next_head["headId"], head["headId"]);
    assert_eq!(next_head["textTail"]["content"], head["textTail"]["content"]);
    assert_eq!(next_head["textTail"]["thinking"], head["textTail"]["thinking"]);
}

#[test]
fn hundreds_of_text_appends_keep_one_head_and_bounded_final_pages_after_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let base = seed(&mut database);
    let mut revision = logical(&database, 11)["projectionRevision"].as_u64().unwrap();
    let suffix = format!("{}😀汉字é\n\\\"", "a".repeat(223));
    let thought = "思考\n";
    let first = append(&mut database, &request(revision, &suffix, thought));
    revision = first["projection_revision"].as_u64().unwrap();
    let head_id = physical(&database)["_projectionHead"]["headId"].clone();
    let old_reader = database.build_read_replica().unwrap();
    for _ in 1..256 {
        let result = append(&mut database, &request(revision, &suffix, thought));
        revision = result["projection_revision"].as_u64().unwrap();
        let stored = physical(&database);
        assert_eq!(stored["_projectionHead"]["headId"], head_id);
        assert!(serde_json::to_vec(&stored).unwrap().len() < 4096);
    }
    assert_eq!(logical(&old_reader, 11)["projection"]["content"], suffix);
    drop(old_reader);
    let mut expected = base;
    expected.insert("content".to_owned(), json!(suffix.repeat(256)));
    expected.insert("thinking".to_owned(), json!(thought.repeat(256)));
    assert_eq!(logical(&database, 11)["projection"], Value::Object(expected.clone()));
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    assert_eq!(logical(&database, 11)["projection"], Value::Object(expected));
    assert_eq!(append(&mut database, &request(revision, "after restart", ""))["applied"], true);
    assert!(logical(&database, 11)["projection"]["content"].as_str().unwrap().ends_with("after restart"));
}

#[test]
fn stale_owner_rollback_structural_fold_and_terminal_preserve_authority() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let base = seed(&mut database);
    let revision = logical(&database, 11)["projectionRevision"].as_u64().unwrap();
    let update = request(revision, "first😀", "thought");
    let mut rolled_back = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&event_record(&database, &mut rolled_back, &update).unwrap()).unwrap()["applied"], true);
    drop(rolled_back);
    assert_eq!(logical(&database, 11)["projection"], Value::Object(base));
    let mut other_owner = database.begin_with_identity_claim_scopes(7, 12).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&event_record(&database, &mut other_owner, &update).unwrap()).unwrap()["applied"], false);
    drop(other_owner);
    append(&mut database, &update);
    let before = logical(&database, 11);
    let mut stale = database.begin_with_identity_claim_scopes(7, 11).unwrap();
    assert!(event_record(&database, &mut stale, &update).is_err());
    drop(stale);
    assert_eq!(logical(&database, 11), before);
    let mut structural = request(revision + 1, "", "");
    structural.slim = false;
    structural.projection_patch = Some(json!({"version": 1, "baseRevision": revision + 1,
        "targetRevision": revision + 2, "operations": [
            {"op": "set", "path": ["model"], "value": "model-after-text"},
        ]}).as_object().unwrap().clone());
    append(&mut database, &structural);
    assert_eq!(logical(&database, 11)["projection"]["content"], "first😀");
    assert_eq!(logical(&database, 11)["projection"]["model"], "model-after-text");
    let stored = physical(&database);
    assert!(stored["_projectionHead"].get("textTail").is_none());
    append(&mut database, &request(revision + 2, "second", ""));
    let mut terminal = request(revision + 3, "", "");
    terminal.terminal = true;
    terminal.status = "completed".to_owned();
    terminal.slim = false;
    terminal.projection_patch = None;
    terminal.projection = logical(&database, 11)["projection"].clone();
    terminal.settlement = json!({"outcome": "completed"}).as_object().unwrap().clone();
    assert_eq!(append(&mut database, &terminal)["applied"], true);
    assert!(physical(&database).get("_projectionHead").is_none());
    assert_eq!(logical(&database, 11)["projection"]["content"], "first😀second");
    assert_eq!(append(&mut database, &request(revision + 4, "late", ""))["applied"], false);
}

#[test]
fn text_pages_split_at_utf8_boundaries_and_reject_missing_payloads() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    seed(&mut database);
    let revision = logical(&database, 11)["projectionRevision"].as_u64().unwrap();
    let content = format!("{}😀\n\\\"{}", "x".repeat(MAX_TURN_PROJECTION_TEXT_PAGE_BYTES - 1), "汉".repeat(20));
    append(&mut database, &request(revision, &content, ""));
    assert_eq!(logical(&database, 11)["projection"]["content"], content);
    let descriptor = projection_head_from_document(&physical(&database)).unwrap().unwrap();
    let mut corruption = database.begin(7, 11).unwrap();
    let key = page_key(&corruption,
        "text-conversation", "text-turn", &descriptor.head_id, "content", 1).unwrap();
    database.entity_delete(&mut corruption, key).unwrap();
    assert!(get(&database, &mut corruption, "text-conversation", "text-turn").is_err());
    drop(corruption);
    assert_eq!(logical(&database, 11)["projection"]["content"], content);
}

fn simulated_fixture() -> (Arc<DeterministicVfs>, AuthorityDatabase, u64) {
    let vfs = Arc::new(DeterministicVfs::new(None));
    vfs.create_dir(Path::new("/data")).unwrap();
    vfs.sync_directory(Path::new("/")).unwrap();
    let mut database = AuthorityDatabase::initialize_with_vfs(
        Path::new("/data"), vfs.clone()).unwrap();
    database.set_write_overlay_for_test(true);
    seed(&mut database);
    let revision = logical(&database, 11)["projectionRevision"].as_u64().unwrap();
    append(&mut database, &request(revision, "prefix", "thought"));
    (vfs, database, revision + 1)
}

#[test]
fn terminal_retires_more_than_sixty_four_pages_and_keeps_old_reader() {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    database.set_write_overlay_for_test(true);
    seed(&mut database);
    let mut revision = logical(&database, 11)["projectionRevision"].as_u64().unwrap();
    let suffix = "x".repeat(MAX_TURN_PROJECTION_TEXT_APPEND_BYTES);
    for _ in 0..18 {
        append(&mut database, &request(revision, &suffix, ""));
        revision += 1;
    }
    let descriptor = projection_head_from_document(&physical(&database)).unwrap().unwrap();
    assert!(descriptor.text_tail.as_ref().unwrap().page_count() > 64);
    let old_reader = database.build_read_replica().unwrap();
    let before = logical(&database, 11);
    let mut terminal = request(revision, "", "");
    terminal.terminal = true;
    terminal.status = "completed".to_owned();
    terminal.slim = false;
    terminal.projection_patch = None;
    terminal.projection = before["projection"].clone();
    terminal.settlement = json!({"outcome": "completed"}).as_object().unwrap().clone();
    append(&mut database, &terminal);
    let mut read = database.begin(7, 11).unwrap();
    let (start, end) = projection_head_range(&read,
        "text-conversation", "text-turn", &descriptor.head_id).unwrap();
    assert!(database.entity_scan(&mut read, &start, &end, 100).unwrap().is_empty());
    assert_eq!(logical(&old_reader, 11), before);
    assert_eq!(logical(&database, 11)["projection"], before["projection"]);
}

#[test]
fn text_pages_and_revision_are_atomic_at_every_commit_io_fault() {
    let suffix = format!("{}😀end", "x".repeat(MAX_TURN_PROJECTION_TEXT_PAGE_BYTES));
    let (vfs, mut baseline, revision) = simulated_fixture();
    let mut transaction = baseline.begin_with_identity_claim_scopes(7, 11).unwrap();
    event_record(&baseline, &mut transaction, &request(revision, &suffix, "more")).unwrap();
    vfs.arm_fault(None).unwrap();
    baseline.commit(transaction).unwrap();
    let trace = vfs.trace().unwrap();
    assert!(!trace.is_empty());
    for (index, operation) in trace.iter().enumerate() {
        let mut actions = vec![FaultAction::ErrorBefore(io::ErrorKind::Other)];
        if *operation == Operation::Write { actions.push(FaultAction::ShortWrite(7)); }
        for action in actions {
            let (vfs, mut database, revision) = simulated_fixture();
            let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            event_record(&database, &mut transaction, &request(revision, &suffix, "more")).unwrap();
            vfs.arm_fault(Some(FaultRule { operation_number: index as u64 + 1, action })).unwrap();
            let acknowledged = database.commit(transaction).is_ok();
            let durable = Arc::new(vfs.fork_durable().unwrap());
            let reopened = AuthorityDatabase::open_with_vfs(Path::new("/data"), durable).unwrap();
            let turn = logical(&reopened, 11);
            let committed = turn["projectionRevision"] == revision + 1;
            assert_eq!(turn["projectionRevision"], if committed { revision + 1 } else { revision });
            assert_eq!(turn["projection"]["content"],
                if committed { format!("prefix{suffix}") } else { "prefix".to_owned() },
                "fault {index} {action:?}");
            assert_eq!(turn["projection"]["thinking"], if committed { "thoughtmore" } else { "thought" });
            assert!(!acknowledged || committed, "lost acknowledged text at fault {index}");
        }
    }
}
