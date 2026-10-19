//! Native snapshot parity for ActiveAttemptPhase. Reads one bounded latest
//! event at the existing authority head; never searches historical phases.
use super::*;

const PHASE_EVENT_MAX_BYTES: usize = 32 * 1024;


#[cfg(test)]
#[path = "snapshot_phase_tests.rs"]
mod tests;

pub(super) fn active_attempt_phase(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turns: &[Value],
    attempts: &Value,
) -> io::Result<Option<Value>> {
    let running = |id: &str| attempts.as_array().is_some_and(|rows| rows.iter().any(|attempt|
        attempt.get("attemptId").and_then(Value::as_str) == Some(id)
            && attempt.get("status").and_then(Value::as_str) == Some("running")));
    let Some(turn) = turns.iter().rev().find(|turn|
        turn.get("laneId").and_then(Value::as_str) == Some("main")
            && turn.get("status").and_then(Value::as_str) == Some("running")
            && turn.get("currentAttemptId").and_then(Value::as_str).is_some_and(running))
    else { return Ok(None); };
    let attempt_id = turn["currentAttemptId"].as_str().expect("selected attempt identity");
    let turn_id = turn.get("turnId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("snapshot phase Turn identity is malformed"))?;
    let head = decode_u64(database.entity_get(transaction,
        &attempt_event_head_key(transaction, attempt_id)?)?, "attempt event head is malformed")?;
    if head == 0 { return Ok(None); }
    let Some(stored) = database.entity_get(transaction,
        &attempt_event_key(transaction, attempt_id, head)?)? else { return Ok(None); };
    // Check the document envelope before fetching an overflow blob. Missing,
    // oversized and cleared latest frames never revive an older phase.
    if crate::versioned_document::stored_document_logical_bytes(
        &stored, ATTEMPT_EVENT_DOCUMENT_IDENTITY)? > PHASE_EVENT_MAX_BYTES as u64 {
        return Ok(None);
    }
    let (_, event, _) = crate::versioned_document::materialize_stored_document_value_bounded(
        database, transaction.tenant_id(), transaction.owner_user_id(), &stored,
        ATTEMPT_EVENT_DOCUMENT_IDENTITY, PHASE_EVENT_MAX_BYTES)?;
    if event.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
        || event.get("turnId").and_then(Value::as_str) != Some(turn_id)
        || event.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
        || event.get("seq").and_then(Value::as_u64) != Some(head) {
        return Err(invalid_data("snapshot phase event identity differs"));
    }
    let Some(phase) = event.get("payload").and_then(|body| body.get("phase"))
        .filter(|phase| phase.is_object()
            && phase.get("phase").and_then(Value::as_str).is_some_and(|text| !text.is_empty()))
    else { return Ok(None); };
    let Some(revision) = event.get("projectionRevision").and_then(Value::as_u64)
        .filter(|revision| turn.get("projectionRevision").and_then(Value::as_u64)
            .is_some_and(|current| *revision <= current))
    else { return Ok(None); };
    Ok(Some(json!({"turnId": turn_id, "attemptId": attempt_id,
        "projectionRevision": revision, "phase": phase})))
}
