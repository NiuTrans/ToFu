//! Canonical attempt-event references in the conversation replay journal.
//!
//! Entry points: encode and materialize. The existing same-owner sync retention
//! index protects the immutable source. Public replay/export expands the source;
//! pruning reads only the compact locator. A distinct document identity makes
//! older readers reject this physical representation rather than publish it.

use super::{attempt_event_key, attempt_sync_reference_key, conversation_sync_event_key, invalid_data,
    invalid_input, AuthorityDatabase, AuthorityTransaction, EntityKey,
    ATTEMPT_EVENT_DOCUMENT_IDENTITY, SYNC_DOCUMENT_IDENTITY};
use crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES;
use serde_json::{Map, Value};
use std::io;

#[cfg(test)]
mod tests;

pub(super) const DOCUMENT_IDENTITY: &str = "conversation_sync_attempt_ref/v1";

fn text<'a>(event: &'a Value, field: &str) -> io::Result<&'a str> {
    event.get(field).and_then(Value::as_str).filter(|s| !s.is_empty())
        .ok_or_else(|| invalid_data("attempt sync reference identity is malformed"))
}

fn sequence(event: &Value) -> io::Result<u64> {
    let payload = event.get("payload").and_then(Value::as_object)
        .filter(|p| p.len() == 1)
        .ok_or_else(|| invalid_data("attempt sync reference payload is malformed"))?;
    let source = payload.get("event").and_then(Value::as_object)
        .filter(|p| p.len() == 1)
        .ok_or_else(|| invalid_data("attempt sync reference source is malformed"))?;
    source.get("seq").and_then(Value::as_u64).filter(|s| *s > 0)
        .ok_or_else(|| invalid_data("attempt sync reference sequence is malformed"))
}

pub(super) fn encode(envelope: &Value, canonical_event_bytes: usize) -> io::Result<Vec<u8>> {
    sequence(envelope)?;
    let raw = serde_json::to_vec(envelope)
        .map_err(|_| invalid_data("attempt sync reference cannot be encoded"))?;
    let placeholder = serde_json::to_vec(&envelope["payload"]["event"])
        .map_err(|_| invalid_data("attempt sync reference source cannot be encoded"))?;
    let expanded_bytes = raw.len().checked_sub(placeholder.len())
        .and_then(|size| size.checked_add(canonical_event_bytes))
        .ok_or_else(|| invalid_data("attempt sync reference size overflows"))?;
    // Keep the former full-envelope put admission and its classified error.
    // A smaller physical locator must not admit an unreadable larger event.
    if expanded_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(invalid_input("invalid versioned document mutation"));
    }
    Ok(raw)
}

/// Expand only after validating namespace, physical/logical identity and source
/// ownership. Legacy envelopes retain their original JSON bytes on export.
pub(super) fn materialize(database: &AuthorityDatabase, tx: &mut AuthorityTransaction,
    key: &EntityKey, stored: &[u8], maximum_bytes: usize, expand: bool,
) -> io::Result<(String, Vec<u8>)> {
    let identity = crate::versioned_document::stored_document_namespace(stored)?;
    if identity == SYNC_DOCUMENT_IDENTITY {
        return crate::versioned_document::materialize_stored_document_bounded(
            database, tx.tenant_id(), tx.owner_user_id(), stored,
            SYNC_DOCUMENT_IDENTITY, maximum_bytes);
    }
    if identity != DOCUMENT_IDENTITY {
        return Err(invalid_data("versioned document identity mismatch"));
    }
    let (logical_key, raw) = crate::versioned_document::materialize_stored_document_bounded(
        database, tx.tenant_id(), tx.owner_user_id(), stored,
        DOCUMENT_IDENTITY, maximum_bytes)?;
    let envelope: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("attempt sync reference is malformed"))?;
    if text(&envelope, "contract")? != "tofu.conversation-sync.event/v1"
        || text(&envelope, "type")? != "attempt.event"
    { return Err(invalid_data("attempt sync reference envelope differs")); }
    let conversation_id = text(&envelope, "conversationId")?;
    let turn_id = text(&envelope, "turnId")?;
    let attempt_id = text(&envelope, "attemptId")?;
    let sync_sequence = envelope.get("syncSeq").and_then(Value::as_u64)
        .filter(|s| *s > 0).ok_or_else(|| invalid_data("attempt sync cursor is malformed"))?;
    let source_sequence = sequence(&envelope)?;
    if logical_key != format!("{conversation_id}\u{1f}{sync_sequence:020}")
        || *key != conversation_sync_event_key(tx, conversation_id, sync_sequence)?
    { return Err(invalid_data("attempt sync reference key differs")); }
    if !expand { return Ok((logical_key, raw)); }
    let protection_key = attempt_sync_reference_key(tx, attempt_id, conversation_id, sync_sequence)?;
    let protection = database.entity_get(tx, &protection_key)?
        .ok_or_else(|| invalid_data("attempt sync protection reference is missing"))?;
    if protection.as_slice() != source_sequence.to_le_bytes() {
        return Err(invalid_data("attempt sync protection reference differs"));
    }
    let source_key = attempt_event_key(tx, attempt_id, source_sequence)?;
    let Some((source, version)) = crate::versioned_document::get_value_with_version(
        database, tx, &source_key, ATTEMPT_EVENT_DOCUMENT_IDENTITY,
        &format!("{attempt_id}\u{1f}{source_sequence:020}"),
    )? else { return Err(invalid_data("referenced attempt event is missing")); };
    if version != 1 {
        return Err(invalid_data("referenced attempt event version differs"));
    }
    // Validate the whole source JSON, including ignored payload fields. Release
    // this temporary view before allocating the public envelope's byte buffer.
    {
        let witness: Value = serde_json::from_slice(&source)
            .map_err(|_| invalid_data("referenced attempt event is malformed"))?;
        if witness.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
            || witness.get("turnId").and_then(Value::as_str) != Some(turn_id)
            || witness.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
            || witness.get("seq").and_then(Value::as_u64) != Some(source_sequence)
        { return Err(invalid_data("referenced attempt event identity differs")); }
    }
    // Splice the validated source bytes without allocating and re-encoding its
    // potentially large payload. This is the former public envelope verbatim.
    let fields: &Map<String, Value> = envelope.as_object().unwrap();
    let mut output = Vec::with_capacity(raw.len().saturating_add(source.len()));
    output.push(b'{');
    for (index, (field, value)) in fields.iter().enumerate() {
        if index > 0 { output.push(b','); }
        serde_json::to_writer(&mut output, field).map_err(|_| invalid_data("sync key cannot be encoded"))?;
        output.push(b':');
        if field == "payload" {
            output.extend_from_slice(b"{\"event\":");
            output.extend_from_slice(&source);
            output.push(b'}');
        } else {
            serde_json::to_writer(&mut output, value).map_err(|_| invalid_data("sync value cannot be encoded"))?;
        }
    }
    output.push(b'}');
    if output.len() > maximum_bytes {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory, "attempt sync event exceeds its bound"));
    }
    Ok((logical_key, output))
}
