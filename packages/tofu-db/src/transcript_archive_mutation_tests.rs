//! Sealed transcript reads must follow the same Turn mutations as point reads.
//! Force sealing before a public command; no scheduler timing or sleeps.
use super::*;

fn command(database: &mut AuthorityDatabase, operation: &str, payload: Value, id: &str) -> Value {
    execute(database, &request(operation, 11, payload, Some(id)), 3_000).unwrap()
}

fn assert_current(database: &mut AuthorityDatabase, turn_id: &str) {
    let point = execute(database, &request("turn.get", 11,
        json!({"conversation_id":"archive-mutation","turn_id":turn_id,"user_id":11}), None), 4_000).unwrap();
    for window in [None, Some(1)] {
        let mut payload = json!({"conv_id":"archive-mutation","user_id":11,"derive_messages":true});
        if let Some(window) = window { payload["message_window"] = json!(window); }
        let conversation = execute(database, &request("conversation.get", 11, payload, None), 4_001).unwrap();
        let message = conversation["messages"].as_array().unwrap().iter()
            .find(|message| message["_turnId"] == turn_id).unwrap();
        assert_eq!(message["_turnStatus"], point["status"], "archive returned a stale lifecycle");
        assert_eq!(message["content"], point["projection"]["content"], "archive returned stale content");
        assert_eq!(message["_projectionRevision"], point["projectionRevision"]);
        assert_eq!(message["_attemptId"], point["currentAttemptId"]);
    }
}

fn archived_mutation(operation: &str) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let pair = command(&mut database, "turn.create_pair", json!({
        "conversation_id":"archive-mutation","user_id":11,"command_id":"create",
        "input_projection":{"content":"question"},"conversation_defaults":{"allowCreate":true},"now":1_000
    }), "create-receipt");
    let attempt = pair["attempt"]["attemptId"].as_str().unwrap();
    let turn = pair["turn"]["turnId"].as_str().unwrap();
    command(&mut database, "turn.attempt.bind", json!({"attempt_id":attempt,"task_id":"archive-task","user_id":11}), "bind");
    if operation != "start" {
        command(&mut database, "turn.event.record", json!({"attempt_id":attempt,"task_id":"archive-task",
            "user_id":11,"projection":{"content":"before"},"now":2_000}), "before");
    }
    if operation == "retry" {
        command(&mut database, "turn.event.record", json!({"attempt_id":attempt,"task_id":"archive-task",
            "user_id":11,"terminal":true,"status":"completed","settlement":{"outcome":"completed","cause":"test"},
            "projection":{"content":"completed before retry"},"now":2_000}), "before-retry");
    }
    let mut transaction = database.begin(7, 11).unwrap();
    let (messages, count, _, _) = crate::turn::legacy_messages(&database, &mut transaction,
        "archive-mutation", 0, None, &mut || false).unwrap();
    crate::transcript_archive::maybe_seal(&database, &mut transaction, "archive-mutation",
        count as u64, messages, 2_001).unwrap();
    database.commit(transaction).unwrap();
    assert_current(&mut database, turn);
    match operation {
        "event" | "terminal" => {
            let mut payload = json!({"attempt_id":attempt,"task_id":"archive-task","user_id":11,
                "projection":{"content":"after"},"now":3_000});
            if operation == "terminal" {
                payload["terminal"] = json!(true); payload["status"] = json!("completed");
                payload["settlement"] = json!({"outcome":"completed","cause":"test","resumeOptions":[]});
            }
            command(&mut database, "turn.event.record", payload, "after");
        }
        "visible" => { command(&mut database, "turn.visible.sync", json!({
            "conversation_id":"archive-mutation","user_id":11,"attempt_id":attempt,
            "root_turn_id":turn,"messages":[{"role":"assistant","content":"before","finishReason":"stop"}]
        }), "visible"); }
        "recover" => { command(&mut database, "turn.recover", json!({"user_id":11,"created_before_ms":5_000}), "recover"); }
        "start" => { command(&mut database, "turn.attempt.start", json!({"user_id":11,"attempt_id":attempt,"task_id":"archive-task"}), "start"); }
        "retry" => {
            let point = execute(&mut database, &request("turn.get", 11,
                json!({"conversation_id":"archive-mutation","turn_id":turn,"user_id":11}), None), 3_000).unwrap();
            command(&mut database, "turn.attempt.create", json!({"user_id":11,
                "conversation_id":"archive-mutation","turn_id":turn,"command_id":"retry",
                "operation":"regenerate","target_actor":"assistant","target_kind":"reply","expected_projection_revision":point["projectionRevision"],"config":{}}), "retry");
        }
        _ => panic!("unknown fixture mutation"),
    }
    assert_current(&mut database, turn);
    drop(database);
    let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
    assert_current(&mut reopened, turn);
}

#[test]
fn sealed_transcript_follows_streamed_event() { archived_mutation("event"); }
#[test]
fn sealed_transcript_follows_terminal_event() { archived_mutation("terminal"); }
#[test]
fn sealed_transcript_follows_visible_phase_settlement() { archived_mutation("visible"); }
#[test]
fn sealed_transcript_follows_restart_recovery() { archived_mutation("recover"); }
#[test]
fn sealed_transcript_follows_attempt_start() { archived_mutation("start"); }
#[test]
fn sealed_transcript_follows_new_attempt_on_settled_turn() { archived_mutation("retry"); }
