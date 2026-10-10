//! Offline task-event stream restoration for the exclusive operator importer.
//! Live writers append through `indexed_stream::append` with a caller-assigned
//! application sequence and the native path already enforces insert-or-match
//! idempotency on `(task_id, sequence, payload_digest)`; this path forwards the
//! caller-explicit sequence, type, payload bytes, and timestamp verbatim. No
//! sequence allocation or payload recomputation happens at this boundary.

use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::io;

use super::{
    append, identity_prefix, index_key, stream_key, AppendRequest, IndexEntry, INDEX_NAMESPACE,
};
use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_ENTITY_KEY_BYTES, MAX_MIGRATION_DOCUMENT_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS,
};

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message.to_string())
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message.to_string())
}

fn unhex(value: &str, maximum: usize) -> io::Result<Vec<u8>> {
    if value.len() > maximum.saturating_mul(2)
        || !value.len().is_multiple_of(2)
        || !value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(invalid_input("invalid storage event hex"));
    }
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16)
                .map_err(|_| invalid_input("invalid storage event hex"))
        })
        .collect()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Restore {
    tenant_id: u64,
    owner_user_id: u64,
    task_id: String,
    sequence: u64,
    event_type: String,
    payload_json_hex: String,
    created_at_ms: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    tenant_id: u64,
    owner_user_id: u64,
    task_id: String,
    sequence: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scan {
    tenant_id: u64,
    owner_user_id: u64,
    task_id: Option<String>,
    cursor: Option<String>,
}

fn scope(tx: &AuthorityTransaction, tenant: u64, owner: u64) -> io::Result<()> {
    if tenant != tx.tenant_id() || owner != tx.owner_user_id() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "storage event scope differs",
        ));
    }
    Ok(())
}

fn cursor_start(cursor: Option<&str>) -> io::Result<Vec<u8>> {
    let Some(cursor) = cursor else {
        return Ok(Vec::new());
    };
    let Some(hex) = cursor.strip_prefix("storage-events:") else {
        return Err(invalid_input("invalid storage event catalog cursor"));
    };
    // The cursor names the last returned key; resume strictly after it.
    let mut raw = unhex(hex, usize::MAX)?;
    if raw.len() < MAX_ENTITY_KEY_BYTES {
        raw.push(0);
    } else {
        let index = raw
            .iter()
            .rposition(|b| *b != 255)
            .ok_or_else(|| invalid_input("storage event catalog cursor has no successor"))?;
        raw[index] += 1;
        raw.truncate(index + 1);
    }
    Ok(raw)
}
fn validate_task_id(task_id: &str) -> io::Result<()> {
    if task_id.is_empty() || task_id.contains('\0') || task_id.chars().count() > 1024 {
        return Err(invalid_input("invalid storage event identity"));
    }
    Ok(())
}

fn read(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    task_id: &str,
    sequence: u64,
) -> io::Result<Option<(IndexEntry, Vec<u8>)>> {
    let key = index_key(tx.tenant_id(), tx.owner_user_id(), task_id, sequence)?;
    let Some(raw) = database.entity_get(tx, &key)? else {
        return Ok(None);
    };
    let entry = IndexEntry::decode(&raw)?;
    if entry.application_sequence != sequence {
        return Err(invalid_data("storage event index identity differs"));
    }
    let stream = stream_key(tx.tenant_id(), tx.owner_user_id(), task_id)?;
    let positions = BTreeSet::from([entry.physical_sequence]);
    let events = database.stream_read_positions_in_transaction(tx, &stream, &positions)?;
    let event = events
        .get(&entry.physical_sequence)
        .ok_or_else(|| invalid_data("storage event payload is missing"))?;
    Ok(Some((
        entry,
        super::materialize_event_payload(database, tx, task_id, entry, event)?.into_owned(),
    )))
}

fn get(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    input: &Identity,
) -> io::Result<Value> {
    validate_task_id(&input.task_id)?;
    match read(database, tx, &input.task_id, input.sequence)? {
        Some((_, payload)) => {
            let hex: String = payload.iter().map(|b| format!("{b:02x}")).collect();
            Ok(json!({
                "tenant_id": tx.tenant_id(),
                "owner_user_id": tx.owner_user_id(),
                "task_id": input.task_id,
                "sequence": input.sequence,
                "payload_json_hex": hex,
            }))
        }
        None => Ok(Value::Null),
    }
}

fn scan(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    request: &Scan,
) -> io::Result<Value> {
    if let Some(task_id) = request.task_id.as_deref() {
        validate_task_id(task_id)?;
    }
    // Only the application-sequence index family (tag 0) is catalogued here;
    // the sibling physical/type/retention families share INDEX_NAMESPACE but
    // are not addressable by (task_id, sequence) and must be excluded.
    let mut family = Vec::from([0u8]);
    if let Some(task_id) = request.task_id.as_deref() {
        family.extend_from_slice(&identity_prefix(task_id)?);
    }
    let suffix = cursor_start(request.cursor.as_deref())?;
    if !suffix.is_empty() && !suffix.starts_with(&family) {
        return Err(invalid_input("storage event cursor scope differs"));
    }
    let scoped = if suffix.is_empty() { family.clone() } else { suffix };
    let start = EntityKey::new(
        tx.tenant_id(),
        tx.owner_user_id(),
        INDEX_NAMESPACE,
        &scoped,
    )?;
    let (_, end) = EntityKey::prefix_range(tx.tenant_id(), tx.owner_user_id(), INDEX_NAMESPACE, &family)?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (key, raw) in &page {
        let entry = IndexEntry::decode(raw)?;
        // Recover the task id from the physical key: [0] + u16be length + id.
        let bytes = key.key_bytes();
        if bytes.len() < 3 || bytes[0] != 0 {
            return Err(invalid_data("storage event catalog key is malformed"));
        }
        let declared = u16::from_be_bytes([bytes[1], bytes[2]]) as usize;
        let task_bytes = bytes
            .get(3..3 + declared)
            .ok_or_else(|| invalid_data("storage event catalog key is malformed"))?;
        let task_id = std::str::from_utf8(task_bytes)
            .map_err(|_| invalid_data("storage event catalog key is not UTF-8"))?;
        if key != &index_key(tx.tenant_id(), tx.owner_user_id(), task_id, entry.application_sequence)? {
            return Err(invalid_data("storage event catalog identity differs"));
        }
        rows.push(json!({
            "tenant_id": tx.tenant_id(),
            "owner_user_id": tx.owner_user_id(),
            "task_id": task_id,
            "sequence": entry.application_sequence,
        }));
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
        Some(format!("storage-events:{hex}"))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}

const MAX_BATCH_ROWS: usize = 256;
const MAX_BATCH_EVENT_BYTES: usize = 8 * 1024 * 1024;

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let mut tx = database.begin(tenant, owner)?;
    match operation {
        "migration.storage.event.restore" => {
            let row: Restore = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid storage event restore request"))?;
            scope(&tx, row.tenant_id, row.owner_user_id)?;
            validate_task_id(&row.task_id)?;
            let payload_json = unhex(&row.payload_json_hex, MAX_MIGRATION_DOCUMENT_BYTES)?;
            let result = append(
                database,
                &mut tx,
                &AppendRequest {
                    task_id: row.task_id,
                    application_sequence: row.sequence,
                    event_type: row.event_type,
                    payload_json,
                    created_at_ms: row.created_at_ms,
                },
            )?;
            let result: Value = serde_json::from_slice(&result)
                .map_err(|_| invalid_data("storage event append result is malformed"))?;
            if result.get("inserted").and_then(Value::as_bool) == Some(true) {
                database.commit(tx)?;
            }
            Ok(result)
        }
        "migration.storage.event.restore_batch" => {
            // Bounded run of rows under one transaction: same scope/identity/
            // insert-or-match semantics per row as the single-row op, with
            // the commit and round trip amortized. A failed row aborts the
            // whole batch; idempotent replay makes partial application
            // impossible to observe.
            #[derive(Deserialize)]
            #[serde(deny_unknown_fields)]
            struct Batch {
                rows: Vec<Restore>,
            }
            let batch: Batch = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid storage event batch request"))?;
            if batch.rows.is_empty() || batch.rows.len() > MAX_BATCH_ROWS {
                return Err(invalid_input("storage event batch exceeds its bounded range"));
            }
            let mut decoded = Vec::with_capacity(batch.rows.len());
            let mut total = 0_usize;
            for row in batch.rows {
                scope(&tx, row.tenant_id, row.owner_user_id)?;
                validate_task_id(&row.task_id)?;
                let payload_json = unhex(&row.payload_json_hex, MAX_MIGRATION_DOCUMENT_BYTES)?;
                total = total
                    .checked_add(payload_json.len())
                    .filter(|total| *total <= MAX_BATCH_EVENT_BYTES)
                    .ok_or_else(|| invalid_input("storage event batch exceeds its byte budget"))?;
                decoded.push((row, payload_json));
            }
            let mut inserted = Vec::with_capacity(decoded.len());
            let mut any = false;
            for (row, payload_json) in decoded {
                let result = append(
                    database,
                    &mut tx,
                    &AppendRequest {
                        task_id: row.task_id,
                        application_sequence: row.sequence,
                        event_type: row.event_type,
                        payload_json,
                        created_at_ms: row.created_at_ms,
                    },
                )?;
                let result: Value = serde_json::from_slice(&result)
                    .map_err(|_| invalid_data("storage event append result is malformed"))?;
                any |= result.get("inserted").and_then(Value::as_bool) == Some(true);
                inserted.push(result);
            }
            if any {
                database.commit(tx)?;
            }
            Ok(json!({"results":inserted}))
        }
        "migration.storage.event.get" => {
            let input: Identity = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid storage event get request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            get(database, &mut tx, &input)
        }
        "migration.storage.event.scan" => {
            let input: Scan = serde_json::from_value(payload)
                .map_err(|_| invalid_input("invalid storage event scan request"))?;
            scope(&tx, input.tenant_id, input.owner_user_id)?;
            scan(database, &mut tx, &input)
        }
        _ => Err(invalid_input("unknown storage event migration operation")),
    }
}

#[cfg(test)]
#[path = "migration_tests.rs"]
mod tests;
