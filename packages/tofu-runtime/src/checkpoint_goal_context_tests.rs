//! Read-port contract: accepted ancestry, exact event bytes and bounded refusal.
use super::*;
use std::collections::BTreeMap;
#[derive(Default)]
struct Authority {
    runs: BTreeMap<String, (Value, Value)>,
    events: BTreeMap<(String, u64), Value>,
    attempts: BTreeMap<String, Value>,
    turns: BTreeMap<String, Value>,
    reads: usize,
}
impl ReadAuthority for Authority {
    fn owner(&self) -> u64 {
        7
    }
    fn event(&mut self, run: &str, label: &str, sequence: u64) -> io::Result<Option<Value>> {
        assert_eq!(label, "3");
        self.reads += 1;
        Ok(self.events.get(&(run.into(), sequence)).cloned())
    }
    fn run(&mut self, run: &str, label: &str) -> io::Result<Option<(Value, Value)>> {
        assert_eq!(label, "3");
        self.reads += 1;
        Ok(self.runs.get(run).cloned())
    }
    fn attempt(&mut self, id: &str) -> io::Result<Option<Value>> {
        self.reads += 1;
        Ok(self.attempts.get(id).cloned())
    }
    fn turn(&mut self, conversation: &str, id: &str) -> io::Result<Option<Value>> {
        assert_eq!(conversation, "conversation");
        self.reads += 1;
        Ok(self.turns.get(id).cloned())
    }
}
fn fixture() -> Authority {
    let mut value = Authority::default();
    for (run, status, cursor) in [("goal_source", "error", 2), ("goal_target", "running", 0)] {
        value.runs.insert(run.into(),(json!({"id":run,"owner":7,"tenant_label":"3",
            "created_by":"chat_goal_mode","orch_id":"chat-goal:conversation","input":"objective","definition":{"same":true}}),
            json!({"status":status,"next_event_sequence":cursor,"error":{"kind":"worker_lost","source":"orchestration.startup_recovery"}})));
    }
    let limits = json!({"max_tokens":null,"max_tool_calls":null,"deadline":null});
    value.events.insert(("goal_source".into(),0),json!({"type":"execution_journal_start","launch_metadata":{
        "format":"tofu.goal-execution-launch/v2","launch_id":"launch","owner_user_id":7,"tenant_id":"3",
        "run_id":"goal_source","task_id":"source","conversation_id":"conversation","objective":"objective",
        "attempt_id":"source-attempt","turn_id":"source-turn"}}));
    value.events.insert(
        ("goal_source".into(), 1),
        json!({"type":"execution_account_frontier","launch_id":"launch",
        "frontier":{"snapshot":{"owner_user_id":7,"root_task_id":"source","limits":limits}}}),
    );
    value.attempts.insert(
        "source-attempt".into(),
        json!({"attemptId":"source-attempt","taskId":"source",
        "turnId":"source-turn","conversationId":"conversation","status":"interrupted","config":{}}),
    );
    value.attempts.insert("target-attempt".into(),json!({"attemptId":"target-attempt","taskId":"target",
        "turnId":"target-turn","conversationId":"conversation","status":"running","operation":"generate",
        "commandId":format!("goal-recovery-{:x}",Sha256::digest(b"goal_source")),
        "config":{"_goalRecoverySourceRunId":"goal_source","_goalObjective":"objective","_turnPrincipalTenantId":"3"}}));
    value.turns.insert("target-turn".into(),json!({"currentAttemptId":"target-attempt","status":"running","parentTurnId":"input","laneId":"lane"}));
    value.turns.insert(
        "input".into(),
        json!({"actor":"virtual_user","kind":"continuation","parentTurnId":"source-turn"}),
    );
    value.turns.insert(
        "source-turn".into(),
        json!({"currentAttemptId":"source-attempt","laneId":"lane"}),
    );
    value
}
fn inspect(value: &mut Authority, remaining: &mut usize) -> io::Result<Value> {
    context(
        value,
        "goal_source",
        "target-attempt",
        "3",
        2,
        0,
        1,
        None,
        remaining,
    )
}
#[test]
fn accepted_read_port_returns_exact_context_without_changing_records() {
    let mut authority = fixture();
    let mut remaining = 16384;
    let source = authority.runs["goal_source"].clone();
    let target = authority.runs["goal_target"].clone();
    let expected = json!({"source":source.0,"source_state":source.1,"target":target.0,"target_state":target.1,
        "launch":authority.events[&("goal_source".into(),0)],"account":authority.events[&("goal_source".into(),1)],
        "source_attempt":authority.attempts["source-attempt"],"accepted":authority.attempts["target-attempt"],
        "chain":[{"attempt":authority.attempts["target-attempt"],"turn":authority.turns["target-turn"],
            "input":authority.turns["input"],"parent":authority.turns["source-turn"]}]});
    let actual = inspect(&mut authority, &mut remaining).unwrap();
    assert_eq!(
        serde_json::to_vec(&actual).unwrap(),
        serde_json::to_vec(&expected).unwrap()
    );
    assert_eq!(authority.runs["goal_source"], source);
    assert_eq!(authority.runs["goal_target"], target);
    assert!(remaining < 16384);
    assert_eq!(authority.reads, 9);
    let exact = 16384 - remaining;
    assert!(inspect(&mut authority, &mut exact.clone()).is_ok());
    assert_eq!(
        inspect(&mut authority, &mut (exact - 1))
            .unwrap_err()
            .kind(),
        io::ErrorKind::OutOfMemory
    );
}
#[test]
fn malformed_run_or_forged_successor_never_produces_a_context() {
    for fault in [
        "owner", "tenant", "run", "cursor", "input", "human", "command", "limits", "admitted",
    ] {
        let mut authority = fixture();
        match fault {
            "owner" => authority.runs.get_mut("goal_source").unwrap().0["owner"] = json!(8),
            "tenant" => {
                authority.runs.get_mut("goal_source").unwrap().0["tenant_label"] = json!("foreign")
            }
            "run" => authority.runs.get_mut("goal_source").unwrap().0["id"] = json!("different"),
            "cursor" => {
                authority.runs.get_mut("goal_source").unwrap().1["next_event_sequence"] =
                    Value::Null
            }
            "input" => authority
                .runs
                .get_mut("goal_source")
                .unwrap()
                .0
                .as_object_mut()
                .unwrap()
                .remove("input")
                .map(|_| ())
                .unwrap(),
            "human" => authority.turns.get_mut("input").unwrap()["actor"] = json!("user"),
            "command" => {
                authority.attempts.get_mut("target-attempt").unwrap()["commandId"] = json!("forged")
            }
            "limits" => {
                authority.attempts.get_mut("target-attempt").unwrap()["config"]["executionLimits"] =
                    json!({"max_tokens":100})
            }
            "admitted" => {
                authority.runs.get_mut("goal_target").unwrap().1["next_event_sequence"] = json!(1);
                authority.events.insert(
                    ("goal_target".into(), 0),
                    json!({"type":"execution_effect_started"}),
                );
            }
            _ => unreachable!(),
        }
        assert!(inspect(&mut authority, &mut 16384).is_err(), "{fault}");
    }
}
#[test]
fn chunked_logical_witness_retains_bytes_and_refuses_changed_digest_or_budget() {
    let mut authority = Authority::default();
    let body =
        serde_json::to_vec(&json!({"type":"execution_journal_start","content":"中文"})).unwrap();
    let digest = format!("{:x}", Sha256::digest(&body));
    authority.events.insert(
        ("run".into(), 0),
        json!({"type":"execution_journal_payload_chunk","sha256":digest,"index":0,
        "data":base64::engine::general_purpose::STANDARD.encode(&body)}),
    );
    authority.events.insert(("run".into(),1),json!({"type":"execution_journal_start","goal_execution_archive":"tofu.goal-execution-launch/v2",
        "payload_ref":{"chunks":1,"byte_length":body.len(),"sha256":digest}}));
    let mut remaining = 8192;
    assert_eq!(
        serde_json::to_vec(&logical_event(&mut authority, "run", "3", 1, &mut remaining).unwrap())
            .unwrap(),
        body
    );
    let needed = 8192 - remaining;
    assert_eq!(
        logical_event(&mut authority, "run", "3", 1, &mut (needed - 1))
            .unwrap_err()
            .kind(),
        io::ErrorKind::OutOfMemory
    );
    authority.events.get_mut(&("run".into(), 0)).unwrap()["data"] =
        json!(base64::engine::general_purpose::STANDARD.encode(vec![b'x'; body.len()]));
    assert_eq!(
        logical_event(&mut authority, "run", "3", 1, &mut 8192)
            .unwrap_err()
            .kind(),
        io::ErrorKind::InvalidData
    );
}
#[test]
fn one_event_budget_refusal_stops_before_following_the_continuation_chain() {
    let mut authority = fixture();
    assert_eq!(
        inspect(&mut authority, &mut 1).unwrap_err().kind(),
        io::ErrorKind::OutOfMemory
    );
    assert_eq!(authority.reads, 2);
}
