//! Restart recovery must preserve a patch-grown Turn beyond the live-write bound.
//! Exercise public semantic admission/execution against a reopened native authority.

use serde_json::{json, Value};
use tofu_db::authority::AuthorityDatabase;
use tofu_db::generated_storage_operations::STORAGE_SCHEMA_VERSION;
use tofu_db::generated_tofudb_ir::{MAX_LEGACY_DOCUMENT_BYTES, MAX_TURN_SNAPSHOT_BYTES};
use tofu_db::protocol::{NegotiatedProtocol, Request, PROTOCOL_VERSION};
use tofu_db::semantic::{admit_request, AuthenticatedScope};
use tofu_db::semantic_executor::execute_admitted_request;

fn execute(
    database: &mut AuthorityDatabase,
    operation: &str,
    payload: Value,
    command_id: Option<&str>,
    now_ms: u64,
) -> Value {
    let request = Request {
        correlation_id: [3; 16],
        deadline_unix_ms: 10_000_000_000,
        owner_id: 11,
        tenant_id: Some(7),
        command_id: command_id.map(str::to_owned),
        schema_id: STORAGE_SCHEMA_VERSION,
        operation: operation.to_owned(),
        payload: serde_json::to_vec(&payload).unwrap(),
    };
    // Each live request fits the ordinary wire bound; only the accumulated
    // projection crosses the generic document bound.
    let admitted = admit_request(
        &request,
        NegotiatedProtocol {
            protocol_version: PROTOCOL_VERSION,
            schema_id: STORAGE_SCHEMA_VERSION,
        },
        AuthenticatedScope {
            owner_id: request.owner_id,
            tenant_id: request.tenant_id,
        },
        now_ms,
    )
    .unwrap();
    let response = execute_admitted_request(database, admitted, now_ms)
        .unwrap_or_else(|error| panic!("{operation} failed: {error:?}"));
    serde_json::from_slice(&response).unwrap()
}

fn assert_restart_recovery(append_bytes: usize, crosses_eight_mib_fixture: bool) {
    let directory = tempfile::tempdir().unwrap();
    let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
    let pair = execute(
        &mut database,
        "turn.create_pair",
        json!({
            "conversation_id": "large-recovery-conversation",
            "user_id": 11,
            "command_id": "recovery-pair",
            "input_projection": {"content": "question"},
            "conversation_defaults": {"allowCreate": true},
            "now": 1_000
        }),
        Some("recovery-pair"),
        1_000,
    );
    let attempt_id = pair["attempt"]["attemptId"].as_str().unwrap();
    let turn_id = pair["turn"]["turnId"].as_str().unwrap();
    execute(
        &mut database,
        "turn.attempt.bind",
        json!({"attempt_id": attempt_id, "task_id": "recovery-task", "user_id": 11}),
        Some("recovery-bind"),
        1_100,
    );

    let initial_content = "a".repeat(15 * 1024 * 1024 / 2); // 7.5 MiB
    let appended_content = "b".repeat(append_bytes);
    let recorded = execute(
        &mut database,
        "turn.event.record",
        json!({
            "attempt_id": attempt_id,
            "task_id": "recovery-task",
            "user_id": 11,
            "projection": {
                "content": initial_content,
                "thinking": "retained reasoning",
                "segments": [],
                "evidence": {"source": "restart-regression", "ordinal": 7}
            },
            "now": 1_200
        }),
        Some("recovery-initial-projection"),
        1_200,
    );
    assert_eq!(recorded["applied"], true);
    let base_revision = recorded["projection_revision"].as_u64().unwrap();
    let patched = execute(
        &mut database,
        "turn.event.record",
        json!({
            "attempt_id": attempt_id,
            "task_id": "recovery-task",
            "user_id": 11,
            "projection_patch": {
                "version": 1,
                "baseRevision": base_revision,
                "targetRevision": base_revision + 1,
                "operations": [
                    {"op": "append_text", "path": ["content"], "value": appended_content}
                ]
            },
            "now": 1_300
        }),
        Some("recovery-projection-patch"),
        1_300,
    );
    assert_eq!(patched["applied"], true);
    let get_turn = json!({
        "conversation_id": "large-recovery-conversation",
        "turn_id": turn_id,
        "user_id": 11
    });
    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    let before = execute(&mut database, "turn.get", get_turn.clone(), None, 1_500);
    assert_eq!(before["status"], "running");
    assert_eq!(before["projectionRevision"], base_revision + 1);
    let expected_content = initial_content + &appended_content;
    assert!(before["projection"]["content"].as_str() == Some(expected_content.as_str()));
    let snapshot_bytes = serde_json::to_vec(&before).unwrap().len();
    assert_eq!(
        snapshot_bytes > MAX_LEGACY_DOCUMENT_BYTES,
        crosses_eight_mib_fixture,
        "snapshot size: {snapshot_bytes} bytes"
    );
    assert!(snapshot_bytes < MAX_TURN_SNAPSHOT_BYTES);

    // Default recovery byte budgeting must still admit one oversized row.
    assert_eq!(
        execute(
            &mut database,
            "turn.recover",
            json!({"user_id": 11}),
            Some("restart-recovery"),
            2_000,
        ),
        json!({"recovered": 1, "remaining": 0})
    );
    let recovered = execute(&mut database, "turn.get", get_turn.clone(), None, 2_100);
    assert_eq!(recovered["status"], "interrupted");
    assert_eq!(recovered["settlement"]["cause"], "server_restart");
    assert_eq!(recovered["projectionRevision"], base_revision + 2);
    assert_eq!(recovered["currentAttemptId"], attempt_id);
    assert!(
        recovered["projection"] == before["projection"],
        "recovery changed projection content"
    );
    let get_attempt = json!({"attempt_id": attempt_id, "user_id": 11});
    let recovered_attempt = execute(
        &mut database,
        "turn.attempt.get",
        get_attempt.clone(),
        None,
        2_100,
    );
    assert_eq!(recovered_attempt["status"], "interrupted");
    assert_eq!(recovered_attempt["settledAt"], 2_000);
    // Pair creation + bind + full projection + patch precede one terminal event.
    let get_terminal_events = json!({
        "attempt_id": attempt_id, "user_id": 11, "after": 4, "projection_mode": "patch"
    });
    let terminal_events = execute(
        &mut database,
        "turn.events.list",
        get_terminal_events.clone(),
        None,
        2_100,
    );
    assert_eq!(terminal_events.as_array().unwrap().len(), 1);
    assert_eq!(
        terminal_events[0]["payload"]["settlement"],
        recovered["settlement"]
    );

    drop(database);
    let mut database = AuthorityDatabase::open(directory.path()).unwrap();
    // Neither repeating the same request nor issuing a new recovery command
    // may mutate the settled Turn or duplicate its terminal event.
    for command_id in ["restart-recovery", "restart-recovery-again"] {
        assert_eq!(
            execute(
                &mut database,
                "turn.recover",
                json!({"user_id": 11}),
                Some(command_id),
                3_000
            ),
            json!({"recovered": 0, "remaining": 0})
        );
    }
    assert!(execute(&mut database, "turn.get", get_turn, None, 3_100) == recovered);
    assert_eq!(
        execute(&mut database, "turn.attempt.get", get_attempt, None, 3_100),
        recovered_attempt
    );
    assert_eq!(
        execute(
            &mut database,
            "turn.events.list",
            get_terminal_events,
            None,
            3_100
        ),
        terminal_events
    );
}

#[test]
fn restart_recovery_preserves_projection_grown_beyond_eight_mib() {
    assert_restart_recovery(3 * 1024 * 1024 / 4, true);
}

#[test]
fn restart_recovery_preserves_projection_below_eight_mib() {
    assert_restart_recovery(1024 * 1024 / 4, false);
}
