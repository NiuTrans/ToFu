//! Offline Conversation sync-head restoration for the exclusive operator
//! importer. Live writers advance the head inside `store_*_sync_event` after
//! recomputing the next sequence; this path claims the caller-explicit
//! `(tenant, owner, conversation)` head and its exact little-endian sequence
//! bytes, preserving them verbatim through insert-or-match. No ownership
//! inference or sequence recomputation happens at this boundary.

use serde::Deserialize;
use serde_json::{json, Value};
use std::io;

use super::{
    conversation_prefix, decode_u64, entity_key, invalid_data, invalid_input,
};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_SYNC_HEAD_NAMESPACE, MIGRATION_SESSION_MAX_PAGE_ROWS,
};

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message.to_string())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Restore {
    tenant_id: u64,
    owner_user_id: u64,
    conversation_id: String,
    sync_sequence: u64,
    updated_at_ms: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    tenant_id: u64,
    owner_user_id: u64,
    conversation_id: String,
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
            "conversation sync head scope differs",
        ));
    }
    Ok(())
}

fn validate_conversation_id(conversation_id: &str) -> io::Result<()> {
    if conversation_id.is_empty()
        || conversation_id.contains('\0')
        || conversation_id.chars().count() > 1024
    {
        return Err(invalid_input("invalid conversation sync head identity"));
    }
    Ok(())
}

fn head_key(tx: &AuthorityTransaction, conversation_id: &str) -> io::Result<EntityKey> {
    entity_key(
        tx,
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )
}

fn document(tx: &AuthorityTransaction, conversation_id: &str, sync_sequence: u64) -> Value {
    json!({
        "tenant_id": tx.tenant_id(),
        "owner_user_id": tx.owner_user_id(),
        "conversation_id": conversation_id,
        "sync_sequence": sync_sequence,
    })
}

fn read(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<u64>> {
    let key = head_key(tx, conversation_id)?;
    match database.entity_get(tx, &key)? {
        None => Ok(None),
        Some(bytes) => {
            if bytes.len() != 8 {
                return Err(invalid_data("conversation sync head is malformed"));
            }
            Ok(Some(decode_u64(
                Some(bytes),
                "conversation sync head is malformed",
            )?))
        }
    }
}

fn restore(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    row: &Restore,
) -> io::Result<bool> {
    validate_conversation_id(&row.conversation_id)?;
    if let Some(existing) = read(database, tx, &row.conversation_id)? {
        if existing != row.sync_sequence {
            return Err(conflict("conversation sync head snapshot differs"));
        }
        return Ok(false);
    }
    let key = head_key(tx, &row.conversation_id)?;
    database.entity_put(tx, key, row.sync_sequence.to_le_bytes().to_vec())?;
    Ok(true)
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Scan,
) -> io::Result<Value> {
    let suffix = super::migration::cursor_start("sync-heads", request.cursor.as_deref())?;
    let start = entity_key(tx, CONVERSATION_SYNC_HEAD_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        tx.owner_user_id(),
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (key, bytes) in &page {
        if bytes.len() != 8 {
            return Err(invalid_data("conversation sync head catalog is malformed"));
        }
        let sync_sequence = decode_u64(
            Some(bytes.clone()),
            "conversation sync head catalog is malformed",
        )?;
        // The physical key is the owner/tenant-scoped conversation prefix;
        // strip the 2-byte big-endian length to recover the conversation id.
        let raw = key.key_bytes();
        if raw.len() < 2 {
            return Err(invalid_data("conversation sync head key is malformed"));
        }
        let declared = u16::from_be_bytes([raw[0], raw[1]]) as usize;
        let conversation_bytes = raw
            .get(2..2 + declared)
            .ok_or_else(|| invalid_data("conversation sync head key is malformed"))?;
        let conversation_id = std::str::from_utf8(conversation_bytes)
            .map_err(|_| invalid_data("conversation sync head key is not UTF-8"))?;
        if key != &head_key(tx, conversation_id)? {
            return Err(invalid_data("conversation sync head catalog identity differs"));
        }
        rows.push(document(tx, conversation_id, sync_sequence));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let hex: String = page
            .last()
            .unwrap()
            .0
            .key_bytes()
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        Some(format!("sync-heads:{hex}"))
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
        "migration.conversation.sync.head.restore" => {
            let row: Restore = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid conversation sync head restore request"))?;
            scope(&tx, row.tenant_id, row.owner_user_id)?;
            let inserted = restore(database, &mut tx, &row)?;
            if inserted {
                database.commit(tx)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.conversation.sync.head.get" => {
            let input: Identity = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid conversation sync head get request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            validate_conversation_id(&input.conversation_id)?;
            Ok(match read(database, &mut tx, &input.conversation_id)? {
                Some(sync_sequence) => document(&tx, &input.conversation_id, sync_sequence),
                None => Value::Null,
            })
        }
        "migration.conversation.sync.head.scan" => {
            let input: Scan = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid conversation sync head scan request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            scan(database, &mut tx, &input)
        }
        _ => Err(invalid_input("unknown conversation sync head migration operation")),
    }
}

#[cfg(test)]
#[path = "sync_head_migration_tests.rs"]
mod tests;
