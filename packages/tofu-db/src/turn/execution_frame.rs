//! Projection evidence for the production single-event Turn owner, plus an
//! explicit test-only same-attempt frame. Both reuse Turn admission, patches,
//! checkpoint policy and exact publication. The witness reuses an unchanged
//! checkpoint across independent commits; it adds no producer queue or RPC.

use super::*;
#[cfg(test)]
use crate::transaction_ir::TransactionIrStep;

mod witness;
pub(super) use witness::{ProjectionWitness, VerifiedStableSegments, classify_patch, append_from_witness, normalize_for_patch};

/// Exact serialized lengths of the owned projection's top-level fields. Only
/// affected values are encoded after admission; append_text measures its suffix
/// rather than re-encoding an accumulated prefix. This is transaction-local
/// evidence, discarded together with the private projection on any error.
#[cfg(test)]
struct ProjectionBytes {
    values: BTreeMap<String, usize>,
    total: usize,
}

#[cfg(test)]
fn encoded_len(value: &impl Serialize) -> io::Result<usize> {
    serde_json::to_vec(value).map(|bytes| bytes.len())
        .map_err(|_| invalid_data("execution frame value cannot be encoded"))
}

#[cfg(test)]
impl ProjectionBytes {
    fn new(projection: &Map<String, Value>) -> io::Result<Self> {
        let mut value = Self { values: BTreeMap::new(), total: 2 };
        for (key, field) in projection {
            value.replace(key, encoded_len(field)?)?;
        }
        Ok(value)
    }

    fn replace(&mut self, key: &str, bytes: usize) -> io::Result<()> {
        self.total = if let Some(old) = self.values.insert(key.to_owned(), bytes) {
            self.total.checked_sub(old).and_then(|total| total.checked_add(bytes))
        } else {
            self.total.checked_add(encoded_len(&key)? + 1 + usize::from(self.values.len() > 1))
                .and_then(|total| total.checked_add(bytes))
        }.ok_or_else(|| invalid_data("execution frame projection byte count overflow"))?;
        Ok(())
    }

    fn apply(&mut self, patch: &Map<String, Value>) -> io::Result<()> {
        for operation in patch["operations"].as_array().unwrap() {
            let key = operation["path"][0].as_str().unwrap();
            let bytes = match operation["op"].as_str().unwrap() {
                "set" => encoded_len(operation.get("value").unwrap_or(&Value::Null))?,
                "append_text" => {
                    let suffix = operation.get("value").and_then(Value::as_str)
                        .ok_or_else(|| invalid_input("execution frame append must be text"))?;
                    let suffix_bytes = encoded_len(&suffix)?.checked_sub(2)
                        .ok_or_else(|| invalid_data("execution frame string encoding is malformed"))?;
                    self.values.get(key).copied()
                        .and_then(|old| old.checked_add(suffix_bytes))
                        .ok_or_else(|| invalid_input("execution frame append target is missing or too large"))?
                }
                _ => return Err(invalid_input("unsupported execution frame delta")),
            };
            self.replace(key, bytes)?;
        }
        Ok(())
    }
}

#[cfg(test)]
fn eligible(request: &EventRecordRequest) -> bool {
    if request.terminal || request.slim || request.status != "running"
        || request.projection_segments_stable || request.background_command_ack.is_some()
        || !request.settlement.is_empty() || !request.error.is_empty()
        || !request.preceding_task_events.is_empty() || request.event_type != "projection_updated"
        || request.task_event.as_ref().is_some_and(|event| event.task_id != request.task_id
            || !matches!(event.event_type.as_str(), "delta" | "program_output" | "tool_progress" | "phase"))
    { return false; }
    let Some(operations) = request.projection_patch.as_ref()
        .and_then(|patch| patch.get("operations")).and_then(Value::as_array)
    else { return false; };
    operations.iter().all(|operation| {
        let Some(path) = operation.get("path").and_then(Value::as_array) else { return false; };
        path.len() == 1 && matches!(path[0].as_str(), Some("content" | "thinking" | "phase"))
            && matches!(operation.get("op").and_then(Value::as_str), Some("set" | "append_text"))
    })
}

/// Ordinary event_record repairs accepted guidance even when the request only
/// touches text. Prove its shared helper is a no-op on the unchanged toolRounds,
/// using one private copy of that field, never a clone of the large text prefix.
fn require_guidance_repair_free_projection(projection: &Map<String, Value>) -> io::Result<()> {
    let Some(rounds) = projection.get("toolRounds").and_then(Value::as_array) else { return Ok(()); };
    if !rounds.iter().any(|round| round.get("guidanceAnswerCommandId").is_some()) { return Ok(()); }
    let mut candidate = Map::from_iter([("toolRounds".to_owned(), Value::Array(rounds.clone()))]);
    if preserve_guidance_answers(projection, &mut candidate) {
        return Err(invalid_input("execution frame requires guidance receipts that need no repair"));
    }
    Ok(())
}

/// All input is already owned by one validated IR and one durability boundary.
/// The enclosing IR must abandon the transaction on error, exactly as for any
/// other semantic step. No partial responses escape preparation on failure.
#[cfg(test)]
pub(crate) fn record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    steps: &[TransactionIrStep],
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<(usize, Vec<u8>)>> {
    let requests = steps.iter().map(|step| match step {
        TransactionIrStep::TurnEventRecord { request, slot } if eligible(request) => Ok((request, *slot)),
        _ => Err(invalid_input("execution frame contains a non-combinable transition")),
    }).collect::<io::Result<Vec<_>>>()?;
    let Some((first, _)) = requests.first().copied() else {
        return Err(invalid_input("execution frame is empty"));
    };
    if requests.iter().any(|(request, _)| request.attempt_id != first.attempt_id
        || request.task_id != first.task_id || request.flow_root_attempt_id != first.flow_root_attempt_id)
    { return Err(invalid_input("execution frame crosses an attempt or task boundary")); }
    let Some((attempt, loaded)) = load_event_record_state(database, transaction, first, true)? else {
        return requests.iter().map(|(_, slot)| Ok((*slot, encode_response(&json!({"applied": false}))?))).collect();
    };
    if attempt.document.get("status").and_then(Value::as_str) != Some("running")
        || attempt.document.get("taskId").and_then(Value::as_str) != Some(first.task_id.as_str())
    { return Err(invalid_input("execution frame requires its already-running bound attempt")); }
    let LoadedTurnForAttempt { key, physical_version, mut document, projection_head: mut current_head, .. } = loaded;
    let initial_updated_at = document.get("updatedAt").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Turn update timestamp is malformed"))?;
    let mut projection = match document.remove("projection") {
        Some(Value::Object(value)) => value,
        _ => Map::new(),
    };
    require_guidance_repair_free_projection(&projection)?;
    let mut witness = ProjectionBytes::new(&projection)?;
    let mut responses = Vec::with_capacity(requests.len());
    for (request, slot) in &requests {
        if deadline_elapsed() { return Err(io::Error::new(io::ErrorKind::TimedOut, "execution frame deadline exceeded")); }
        let patch = request.projection_patch.as_ref().unwrap();
        let revision = document.get("projectionRevision").and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("Turn projection revision is malformed"))?;
        let next = revision.checked_add(1).ok_or_else(|| invalid_data("Turn revision overflow"))?;
        if patch.get("baseRevision").and_then(Value::as_u64) != Some(revision) {
            return Err(typed_conflict(TurnConflictKind::ProjectionStale));
        }
        if patch.get("targetRevision").and_then(Value::as_u64) != Some(next) {
            return Err(invalid_input("Turn projection patch must advance exactly one revision"));
        }
        let previous = if current_head.is_none() { projection.clone() } else { Map::new() };
        witness.apply(patch)?;
        projection = crate::turn_projection_patch::apply_projection_patch_owned(projection, patch)
            .map_err(|_| invalid_input("Turn projection patch is invalid"))?;
        document.insert("projectionRevision".to_owned(), Value::from(next));
        document.insert("updatedAt".to_owned(), Value::from(request.now_ms));
        document.insert("status".to_owned(), Value::String(request.status.clone()));
        document.insert("settlement".to_owned(), Value::Object(Map::new()));
        validate_settleable_turn_size(&document, &projection, witness.total, &attempt.document, false)?;
        current_head = store_event_projection(database, transaction, request,
            &attempt.conversation_id, document["turnId"].as_str().unwrap(), revision, next,
            current_head.as_ref(), &previous, &projection, witness.total, patch)?;
        responses.push((*slot, publish_event_record(database, transaction, request, EventPublication {
            conversation_id: &attempt.conversation_id, turn: &document, attempt: &attempt.document,
            projection: &projection, projection_bytes: witness.total,
            replay_patch: patch.clone(), starts_attempt: false,
        })?));
    }
    let last = requests.last().unwrap().0;
    let turn_id = document.get("turnId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn identity is malformed"))?.to_owned();
    let lane_id = document.get("laneId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn lane identity is malformed"))?.to_owned();
    let ordinal = document.get("ordinal").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Turn ordinal is malformed"))?;
    let revision = document["projectionRevision"].as_u64().unwrap();
    let created_at = attempt.document.get("createdAt").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
    database.entity_put(transaction, recovery_index_key(transaction, created_at, &last.attempt_id)?,
        recovery_index_value(&attempt.document, witness.total)?)?;
    if let Some(mut head) = current_head {
        head.frame_witness = ProjectionWitness::mint(&projection, revision,
            document.get("actor").and_then(Value::as_str).unwrap_or("assistant"), &last.status)?;
        document.insert("projection".to_owned(), json!({}));
        document.insert("_projectionHead".to_owned(), projection_head_value(&head));
    } else {
        document.insert("projection".to_owned(), Value::Object(projection));
        document.remove("_projectionHead");
    }
    crate::versioned_document::put_with_blob_owner_bounded(database, transaction,
        crate::versioned_document::PutRequest { key: key.clone(), namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&attempt.conversation_id, &turn_id),
            value_json: serde_json::to_vec(&document).map_err(|_| invalid_data("Turn cannot be encoded"))?,
            expected_version: Some(physical_version), updated_at_ms: last.now_ms },
        transaction.owner_user_id(), crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES)?;
    let stored = database.entity_get(transaction, &key)?.ok_or_else(|| invalid_data("staged Turn disappeared"))?;
    store_changed_lane_turn(database, transaction, &attempt.conversation_id, &lane_id, ordinal, stored)?;
    put_lane_compaction_index(database, transaction, &document)?;
    database.entity_delete(transaction, updated_index_key(transaction, &attempt.conversation_id, initial_updated_at, &turn_id)?)?;
    database.entity_put(transaction, updated_index_key(transaction, &attempt.conversation_id, last.now_ms, &turn_id)?,
        encode_updated_index_value(&turn_id, revision)?)?;
    crate::conversation_header::advance_for_turn_event_frame(database, transaction,
        &attempt.conversation_id, last.now_ms, requests.len() as u64)?;
    Ok(responses)
}

#[cfg(test)]
#[path = "../turn_execution_frame_tests.rs"]
mod tests;
