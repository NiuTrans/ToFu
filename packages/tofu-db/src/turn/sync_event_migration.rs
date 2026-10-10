//! Offline Conversation sync-event restoration for the exclusive operator
//! importer. Live writers recompute the envelope and next sequence internally
//! inside `store_*_sync_event`; this path accepts the caller-explicit
//! `sync_sequence` and the caller's exact `event_json` bytes, preserving both
//! verbatim through insert-or-match on the full byte content. It also
//! reproduces the retention/age secondary indexes so the restored target is
//! queryable identically to a natively written one. No envelope or sequence
//! recomputation happens at this boundary.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use super::{
    conversation_prefix, conversation_sync_event_key, entity_key, invalid_data, invalid_input,
    store_sync_retention_indexes, SYNC_DOCUMENT_IDENTITY,
};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_SYNC_EVENT_NAMESPACE, MAX_MIGRATION_DOCUMENT_BYTES,
    MIGRATION_SESSION_MAX_PAGE_ROWS,
};
use crate::versioned_document::PutRequest;

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message.to_string())
}

fn unhex(value: &str, maximum: usize) -> io::Result<Vec<u8>> {
    if value.len() > maximum * 2
        || !value.len().is_multiple_of(2)
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(invalid_input("invalid conversation sync event hex"));
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                .map_err(|_| invalid_input("invalid conversation sync event hex"))
        })
        .collect()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Restore {
    tenant_id: u64,
    owner_user_id: u64,
    conversation_id: String,
    sync_sequence: u64,
    change_type: String,
    turn_id: Option<String>,
    attempt_id: Option<String>,
    event_json_hex: String,
    created_at_ms: i64,
    attempt_sequence: Option<u64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    tenant_id: u64,
    owner_user_id: u64,
    conversation_id: String,
    sync_sequence: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    tenant_id: u64,
    owner_user_id: u64,
    cursor: Option<String>,
}

fn scope(tx: &AuthorityTransaction, tenant: u64, owner: u64) -> io::Result<()> {
    if tenant != tx.tenant_id() || owner != tx.owner_user_id() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "conversation sync event scope differs",
        ));
    }
    Ok(())
}

fn validate_conversation_id(conversation_id: &str) -> io::Result<()> {
    if conversation_id.is_empty()
        || conversation_id.contains('\0')
        || conversation_id.chars().count() > 1024
    {
        return Err(invalid_input("invalid conversation sync event identity"));
    }
    Ok(())
}

fn logical_key(conversation_id: &str, sync_sequence: u64) -> String {
    format!("{conversation_id}\u{1f}{sync_sequence:020}")
}

fn read(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
    sync_sequence: u64,
) -> io::Result<Option<Vec<u8>>> {
    let key = conversation_sync_event_key(tx, conversation_id, sync_sequence)?;
    let Some(stored) = database.entity_get(tx, &key)? else { return Ok(None); };
    let (stored_key, raw) = super::sync_attempt_reference::materialize(
        database, tx, &key, &stored, MAX_MIGRATION_DOCUMENT_BYTES, true)?;
    if stored_key != logical_key(conversation_id, sync_sequence) {
        return Err(invalid_data("conversation sync document key differs"));
    }
    Ok(Some(raw))
}

fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    row: &Restore,
) -> io::Result<bool> {
    validate_conversation_id(&row.conversation_id)?;
    if row.sync_sequence == 0 {
        return Err(invalid_input("conversation sync event sequence is invalid"));
    }
    let event_json = unhex(&row.event_json_hex, MAX_MIGRATION_DOCUMENT_BYTES)?;
    // The bytes must be a JSON object so the retention indexer can read its
    // envelope fields, but the exact byte spelling is preserved verbatim.
    let event: Value = serde_json::from_slice(&event_json)
        .map_err(|_| invalid_input("conversation sync event JSON is invalid"))?;
    if event
        .get("conversationId")
        .and_then(Value::as_str)
        != Some(row.conversation_id.as_str())
        || event.get("syncSeq").and_then(Value::as_u64) != Some(row.sync_sequence)
    {
        return Err(invalid_input("conversation sync event envelope identity differs"));
    }
    let key = conversation_sync_event_key(tx, &row.conversation_id, row.sync_sequence)?;
    if let Some(existing) = read(database, tx, &row.conversation_id, row.sync_sequence)? {
        if existing != event_json {
            return Err(conflict("conversation sync event snapshot differs"));
        }
        return Ok(false);
    }
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        tx,
        PutRequest {
            key,
            namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
            logical_key: logical_key(&row.conversation_id, row.sync_sequence),
            value_json: event_json,
            expected_version: Some(0),
            updated_at_ms: row.created_at_ms.max(0) as u64,
        },
        tx.owner_user_id(),
        MAX_MIGRATION_DOCUMENT_BYTES,
    )?;
    // Reproduce the retention/age secondary indexes from the caller-supplied
    // envelope so the restored target is queryable identically.
    store_sync_retention_indexes(database, tx, &event)?;
    Ok(true)
}

fn document(tx: &AuthorityTransaction, conversation_id: &str, sync_sequence: u64, event_json: &[u8]) -> Value {
    let hex: String = event_json.iter().map(|b| format!("{b:02x}")).collect();
    json!({
        "tenant_id": tx.tenant_id(),
        "owner_user_id": tx.owner_user_id(),
        "conversation_id": conversation_id,
        "sync_sequence": sync_sequence,
        "event_json_hex": hex,
    })
}

fn get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    input: &Identity,
) -> io::Result<Value> {
    validate_conversation_id(&input.conversation_id)?;
    match read(database, tx, &input.conversation_id, input.sync_sequence)? {
        Some(event_json) => Ok(document(
            tx,
            &input.conversation_id,
            input.sync_sequence,
            &event_json,
        )),
        None => Ok(Value::Null),
    }
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Scan,
) -> io::Result<Value> {
    let suffix = super::migration::cursor_start("sync-events", request.cursor.as_deref())?;
    let start = entity_key(tx, CONVERSATION_SYNC_EVENT_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        CONVERSATION_SYNC_EVENT_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (key, stored) in &page {
        let (_, raw) = super::sync_attempt_reference::materialize(
            database, tx, key, stored, MAX_MIGRATION_DOCUMENT_BYTES, true)?;
        let event: Value = serde_json::from_slice(&raw)
            .map_err(|_| invalid_data("conversation sync event catalog is malformed"))?;
        let conversation_id = event
            .get("conversationId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("conversation sync event identity missing"))?;
        let sync_sequence = event
            .get("syncSeq")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation sync event sequence missing"))?;
        if key != &conversation_sync_event_key(tx, conversation_id, sync_sequence)? {
            return Err(invalid_data("conversation sync event catalog identity differs"));
        }
        rows.push(json!({
            "tenant_id": tx.tenant_id(),
            "owner_user_id": tx.owner_user_id(),
            "conversation_id": conversation_id,
            "sync_sequence": sync_sequence,
        }));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = page.last().unwrap().0.key_bytes();
        let hex: String = last.iter().map(|b| format!("{b:02x}")).collect();
        Some(format!("sync-events:{hex}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut tx = database.begin(tenant, owner)?;
    match operation {
        "migration.conversation.sync.event.restore" => {
            let row: Restore = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid conversation sync event restore request"))?;
            scope(&tx, row.tenant_id, row.owner_user_id)?;
            let inserted = restore(database, &mut tx, &row)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.conversation.sync.event.get" => {
            let input: Identity = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid conversation sync event get request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            get(database, &mut tx, &input)
        }
        "migration.conversation.sync.event.scan" => {
            let input: Scan = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid conversation sync event scan request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            scan(database, &mut tx, &input)
        }
        _ => Err(invalid_input(
            "unknown conversation sync event migration operation",
        )),
    }
}

#[cfg(test)]
#[path = "sync_event_migration_tests.rs"]
mod tests;
