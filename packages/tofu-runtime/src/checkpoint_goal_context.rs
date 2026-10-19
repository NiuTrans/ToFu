//! Shared Goal recovery ancestry and bounded immutable event witness reader.
//! Authority adapters supply owner-filtered records from one transaction. This
//! module checks the accepted continuation chain and exact source/target cut;
//! it never opens storage, mutates state, settles debt or grants a lease.
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, io};
const GOAL_CREATED_BY: &str = "chat_goal_mode";

/// Reads must enforce the selected owner/tenant and per-document byte bounds.
/// Return original persisted projections; no inferred or repaired authority.
pub trait ReadAuthority {
    fn owner(&self) -> u64;
    fn event(&mut self, run: &str, label: &str, sequence: u64) -> io::Result<Option<Value>>;
    fn run(&mut self, run: &str, label: &str) -> io::Result<Option<(Value, Value)>>;
    fn attempt(&mut self, id: &str) -> io::Result<Option<Value>>;
    fn turn(&mut self, conversation: &str, id: &str) -> io::Result<Option<Value>>;
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::AlreadyExists, message)
}
fn exhausted(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::OutOfMemory, message)
}
fn cursor_of(state: &Value) -> io::Result<u64> {
    state["next_event_sequence"]
        .as_u64()
        .ok_or_else(|| invalid_data("Goal run cursor is invalid"))
}
fn read_run(
    authority: &mut impl ReadAuthority,
    run: &str,
    label: &str,
) -> io::Result<Option<(Value, Value)>> {
    let Some((core, state)) = authority.run(run, label)? else {
        return Ok(None);
    };
    if core["id"] != run
        || core["owner"].as_u64() != Some(authority.owner())
        || core["tenant_label"] != label
        || ["created_by", "orch_id", "input"]
            .iter()
            .any(|key| !core[*key].is_string())
        || core.get("definition").is_none()
        || !state["status"].is_string()
        || state.get("error").is_none()
    {
        return Err(invalid_data("Goal run identity or projection differs"));
    }
    cursor_of(&state)?;
    Ok(Some((core, state)))
}
fn attempt(authority: &mut impl ReadAuthority, id: &str) -> io::Result<Value> {
    authority
        .attempt(id)?
        .ok_or_else(|| invalid_data("Goal claim attempt missing"))
}
fn read_turn(
    authority: &mut impl ReadAuthority,
    conversation: &str,
    id: &str,
) -> io::Result<Value> {
    authority
        .turn(conversation, id)?
        .ok_or_else(|| invalid_data("Goal claim continuation missing"))
}
fn charge(value: &Value, remaining: &mut usize) -> io::Result<()> {
    let bytes = serde_json::to_vec(value)
        .map_err(|_| invalid_data("Goal claim witness JSON"))?
        .len();
    *remaining = remaining
        .checked_sub(bytes)
        .ok_or_else(|| exhausted("Goal claim evidence byte bound"))?;
    Ok(())
}
pub fn event(
    authority: &mut impl ReadAuthority,
    run: &str,
    label: &str,
    sequence: u64,
    remaining: &mut usize,
) -> io::Result<Option<Value>> {
    let Some(value) = authority.event(run, label, sequence)? else {
        return Ok(None);
    };
    charge(&value, remaining)?;
    Ok(Some(value))
}

pub fn logical_event(
    authority: &mut impl ReadAuthority,
    run: &str,
    label: &str,
    sequence: u64,
    remaining: &mut usize,
) -> io::Result<Value> {
    let value = event(authority, run, label, sequence, remaining)?
        .ok_or_else(|| invalid_data("Goal claim witness disappeared"))?;
    let Some(reference) = value.get("payload_ref") else {
        return Ok(value);
    };
    if !matches!(
        value["goal_execution_archive"].as_str(),
        Some("tofu.goal-execution-launch/v1" | "tofu.goal-execution-launch/v2")
    ) || reference.as_object().is_none_or(|value| value.len() != 3)
    {
        return Err(invalid_data("Goal claim archive reference"));
    }
    let count = reference["chunks"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= sequence)
        .ok_or_else(|| invalid_data("Goal claim chunk count"))?;
    let length = reference["byte_length"]
        .as_u64()
        .and_then(|n| usize::try_from(n).ok())
        .filter(|n| *n > 0 && *n <= *remaining)
        .ok_or_else(|| exhausted("Goal claim payload bound"))?;
    let digest = reference["sha256"]
        .as_str()
        .filter(|s| s.len() == 64)
        .ok_or_else(|| invalid_data("Goal claim payload digest"))?;
    let mut body = Vec::new();
    body.try_reserve_exact(length)
        .map_err(|_| exhausted("Goal claim payload allocation"))?;
    for index in 0..count {
        let chunk = event(authority, run, label, sequence - count + index, remaining)?
            .ok_or_else(|| invalid_data("Goal claim chunk disappeared"))?;
        if chunk["type"] != "execution_journal_payload_chunk"
            || chunk["sha256"] != digest
            || chunk["index"].as_u64() != Some(index)
        {
            return Err(invalid_data("Goal claim chunk identity differs"));
        }
        let data = base64::engine::general_purpose::STANDARD
            .decode(
                chunk["data"]
                    .as_str()
                    .ok_or_else(|| invalid_data("Goal claim chunk data"))?,
            )
            .map_err(|_| invalid_data("Goal claim chunk encoding"))?;
        if data.is_empty()
            || body
                .len()
                .checked_add(data.len())
                .is_none_or(|n| n > length)
        {
            return Err(invalid_data("Goal claim chunk size"));
        }
        body.extend_from_slice(&data);
    }
    if body.len() != length || format!("{:x}", Sha256::digest(&body)) != digest {
        return Err(invalid_data("Goal claim payload digest differs"));
    }
    *remaining = remaining
        .checked_sub(body.len())
        .ok_or_else(|| exhausted("Goal claim decoded byte bound"))?;
    let decoded: Value =
        serde_json::from_slice(&body).map_err(|_| invalid_data("Goal claim payload JSON"))?;
    if !decoded.is_object() || decoded["type"] != value["type"] {
        return Err(invalid_data("Goal claim logical event identity differs"));
    }
    Ok(decoded)
}

pub fn context(
    authority: &mut impl ReadAuthority,
    source_run: &str,
    target_attempt: &str,
    label: &str,
    cursor: u64,
    launch_sequence: u64,
    account_sequence: u64,
    claim: Option<&Value>,
    remaining: &mut usize,
) -> io::Result<Value> {
    let owner = authority.owner();
    let (source, state) = read_run(authority, source_run, label)?
        .ok_or_else(|| invalid_data("Goal claim source missing"))?;
    if state["status"] != "error"
        || state["error"]["kind"] != "worker_lost"
        || state["error"]["source"] != "orchestration.startup_recovery"
        || source["created_by"] != GOAL_CREATED_BY
        || Some(cursor_of(&state)?) != cursor.checked_add(u64::from(claim.is_some()))
        || launch_sequence >= account_sequence
        || account_sequence >= cursor
    {
        return Err(conflict("Goal claim source frontier changed"));
    }
    if let Some(claim) = claim {
        if event(authority, source_run, label, cursor, remaining)?.as_ref() != Some(claim) {
            return Err(conflict("Goal actor claim changed before consumption"));
        }
    }
    let launch = logical_event(authority, source_run, label, launch_sequence, remaining)?;
    let account = logical_event(authority, source_run, label, account_sequence, remaining)?;
    let metadata = &launch["launch_metadata"];
    let text = |key: &str| {
        metadata[key]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| invalid_data("Goal claim launch identity"))
    };
    let source_task = text("task_id")?;
    let conversation = text("conversation_id")?;
    if launch["type"] != "execution_journal_start"
        || account["type"] != "execution_account_frontier"
        || !matches!(
            metadata["format"].as_str(),
            Some("tofu.goal-execution-launch/v1" | "tofu.goal-execution-launch/v2")
        )
        || metadata["launch_id"].as_str().is_none_or(str::is_empty)
        || account["launch_id"] != metadata["launch_id"]
        || metadata["owner_user_id"].as_u64() != Some(owner)
        || metadata["tenant_id"] != label
        || metadata["run_id"] != source_run
        || source_run != format!("goal_{source_task}")
        || source["orch_id"] != format!("chat-goal:{conversation}")
        || metadata["objective"] != source["input"]
        || account["frontier"]["snapshot"]["root_task_id"] != source_task
        || account["frontier"]["snapshot"]["owner_user_id"].as_u64() != Some(owner)
    {
        return Err(invalid_data("Goal claim launch/account binding differs"));
    }
    for sequence in account_sequence + 1..cursor {
        let tail = event(authority, source_run, label, sequence, remaining)?
            .ok_or_else(|| invalid_data("Goal claim source tail gap"))?;
        if matches!(
            tail["type"].as_str(),
            Some("execution_account_frontier" | "execution_recovery_claim")
        ) {
            return Err(conflict(
                "Goal claim account is not the current unclaimed cut",
            ));
        }
    }
    let source_attempt = attempt(authority, text("attempt_id")?)?;
    let accepted = attempt(authority, target_attempt)?;
    let target_task = accepted["taskId"]
        .as_str()
        .filter(|s| !s.is_empty() && *s != source_task)
        .ok_or_else(|| invalid_data("Goal claim successor task"))?;
    let target_run = format!("goal_{target_task}");
    let (target, target_state) = read_run(authority, &target_run, label)?
        .ok_or_else(|| invalid_data("Goal claim successor run missing"))?;
    let limits = |value: &Value| {
        value["config"]
            .get("executionLimits")
            .filter(|v| !v.is_null())
            .cloned()
            .unwrap_or_else(|| json!({"max_tokens":null,"max_tool_calls":null,"deadline":null}))
    };
    if source_attempt["taskId"] != source_task
        || source_attempt["turnId"] != metadata["turn_id"]
        || source_attempt["conversationId"] != conversation
        || !matches!(
            source_attempt["status"].as_str(),
            Some("interrupted" | "truncated" | "failed")
        )
        || accepted["status"] != "running"
        || accepted["operation"] != "generate"
        || accepted["conversationId"] != conversation
        || accepted["attemptId"] != target_attempt
        || accepted["config"]["_goalRecoverySourceRunId"] != source_run
        || accepted["config"]["_goalObjective"] != source["input"]
        || accepted["config"]["_turnPrincipalTenantId"]
            .as_str()
            .unwrap_or("")
            != label
        || limits(&accepted) != limits(&source_attempt)
        || limits(&source_attempt) != account["frontier"]["snapshot"]["limits"]
        || target_state["status"] != "running"
        || target["created_by"] != GOAL_CREATED_BY
        || target["orch_id"] != source["orch_id"]
        || target["definition"] != source["definition"]
        || target["input"] != source["input"]
    {
        return Err(invalid_data("Goal claim has no accepted successor lineage"));
    }
    for sequence in 0..cursor_of(&target_state)? {
        let event = event(authority, &target_run, label, sequence, remaining)?
            .ok_or_else(|| invalid_data("Goal claim successor history gap"))?;
        if matches!(
            event["type"].as_str(),
            Some(
                "execution_effect_started"
                    | "execution_recovery_claim"
                    | "execution_recovery_consumed"
            )
        ) || (claim.is_none() && event["type"] == "execution_journal_ready")
        {
            return Err(invalid_data(
                "Goal claim successor already admitted execution",
            ));
        }
    }
    let mut chain = Vec::new();
    let mut current = accepted.clone();
    let mut visited = BTreeSet::new();
    loop {
        let id = current["turnId"]
            .as_str()
            .ok_or_else(|| invalid_data("Goal claim continuation turn"))?;
        if !visited.insert(id.to_owned()) {
            return Err(invalid_data("Goal claim continuation cycle"));
        }
        let turn = read_turn(authority, conversation, id)?;
        let input = read_turn(
            authority,
            conversation,
            turn["parentTurnId"]
                .as_str()
                .ok_or_else(|| invalid_data("Goal claim parent"))?,
        )?;
        let parent = read_turn(
            authority,
            conversation,
            input["parentTurnId"]
                .as_str()
                .ok_or_else(|| invalid_data("Goal claim predecessor"))?,
        )?;
        let predecessor = parent["currentAttemptId"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| invalid_data("Goal claim predecessor attempt"))?;
        let material = if Some(predecessor) == source_attempt["attemptId"].as_str() {
            source_run.to_owned()
        } else {
            format!("{source_run}:{predecessor}")
        };
        if turn["currentAttemptId"] != current["attemptId"]
            || input["actor"] != "virtual_user"
            || (current["attemptId"] == target_attempt && turn["status"] != "running")
            || input["kind"] != "continuation"
            || parent["laneId"] != turn["laneId"]
            || current["commandId"]
                != format!("goal-recovery-{:x}", Sha256::digest(material.as_bytes()))
        {
            return Err(invalid_data(
                "Goal claim continuation is not the accepted command chain",
            ));
        }
        charge(&turn, remaining)?;
        charge(&input, remaining)?;
        charge(&parent, remaining)?;
        chain.push(json!({"attempt":current,"turn":turn,"input":input,"parent":parent}));
        if predecessor == text("attempt_id")? {
            break;
        }
        current = attempt(authority, predecessor)?;
        if current["config"]["_goalRecoverySourceRunId"] != source_run
            || current["operation"] != "generate"
            || current["conversationId"] != conversation
            || !matches!(
                current["status"].as_str(),
                Some("interrupted" | "truncated" | "failed")
            )
        {
            return Err(invalid_data(
                "Goal claim intermediate handoff is not abandoned",
            ));
        }
        if let Some(task) = current["taskId"].as_str().filter(|value| !value.is_empty()) {
            let run_id = format!("goal_{task}");
            if let Some((core, state)) = read_run(authority, &run_id, label)? {
                if state["status"] != "error"
                    || state["error"]["kind"] != "worker_lost"
                    || state["error"]["source"] != "orchestration.startup_recovery"
                    || core["created_by"] != GOAL_CREATED_BY
                    || core["orch_id"] != source["orch_id"]
                    || core["definition"] != source["definition"]
                    || core["input"] != source["input"]
                {
                    return Err(invalid_data(
                        "Goal claim intermediate run is not abandoned preparation",
                    ));
                }
                for sequence in 0..cursor_of(&state)? {
                    let event = event(authority, &run_id, label, sequence, remaining)?
                        .ok_or_else(|| invalid_data("Goal claim intermediate history gap"))?;
                    if matches!(
                        event["type"].as_str(),
                        Some("execution_journal_ready" | "execution_effect_started")
                    ) {
                        return Err(invalid_data(
                            "Goal claim intermediate run already admitted execution",
                        ));
                    }
                }
                chain.push(json!({"prepared_run":core,"prepared_state":state}));
            }
        }
    }
    let mut context = serde_json::Map::new();
    for (key, value) in [
        ("source", source),
        ("source_state", state),
        ("target", target),
        ("target_state", target_state),
        ("launch", launch),
        ("account", account),
        ("source_attempt", source_attempt),
        ("accepted", accepted),
        ("chain", Value::Array(chain)),
    ] {
        context.insert(key.into(), value);
    }
    let context = Value::Object(context);
    charge(&context, remaining)?;
    Ok(context)
}

#[cfg(test)]
#[path = "checkpoint_goal_context_tests.rs"]
mod tests;
