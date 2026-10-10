//! Offline timer and timer-poll-log snapshot restoration for the exclusive
//! operator importer.
//!
//! The source keeps one row per timer in `storage_timers` and one append-only
//! observability row per poll in `storage_timer_poll_log`. The native
//! authority stores a blob-capable versioned timer document, a tenant-global
//! ID claim, owner-local status/created and conversation indexes, a global
//! active feed, owner-scoped total/active counts, immutable poll documents
//! keyed by a global sequence, a poll time index, logical poll-ID claims, and
//! the global poll sequence. Live creation stamps counters and the poll
//! sequence itself, so replaying through it cannot preserve the exact source
//! counters or poll IDs and is not insert-or-match idempotent. This adapter
//! restores the document, claim, all indexes, and both counts in the same
//! authority transaction; poll rows restore with their exact source poll ID,
//! advancing the global poll sequence to the greatest restored ID so later
//! live polls never collide. Insert-or-match fails closed on a divergent
//! occupant, get reconstructs the exact source snapshot, and scan enumerates
//! the owner-scoped catalogs independently, cross-checking them against the
//! restored counts and sequence. Entry points: the `migration.timer.*` and
//! `migration.timer.poll.*` operations dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_TIMER_DOCUMENT_BYTES, MAX_TIMER_ID_CHARACTERS,
    MAX_TIMER_POLL_DOCUMENT_BYTES, MIGRATION_SESSION_MAX_PAGE_ROWS, TIMER_ACTIVE_COUNT_NAMESPACE,
    TIMER_CONVERSATION_INDEX_NAMESPACE, TIMER_DOCUMENT_NAMESPACE,
    TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE, TIMER_ID_CLAIM_NAMESPACE,
    TIMER_POLL_DOCUMENT_NAMESPACE, TIMER_POLL_ID_CLAIM_NAMESPACE, TIMER_POLL_SEQUENCE_NAMESPACE,
    TIMER_POLL_TIME_INDEX_NAMESPACE, TIMER_STATUS_CREATED_INDEX_NAMESPACE,
    TIMER_TOTAL_COUNT_NAMESPACE,
};
use crate::versioned_document::{self, SnapshotDocument, SnapshotRestoreOutcome};

const LOGICAL_NAMESPACE: &str = "timers";
const COUNT_KEY: &[u8] = b"count";
const POLL_SEQUENCE_KEY: &[u8] = b"sequence";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "timer snapshot differs")
}

fn owner_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}
fn global_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, raw)
}
fn document_key(tx: &AuthorityTransaction, timer_id: &str) -> io::Result<EntityKey> {
    global_key(tx, TIMER_DOCUMENT_NAMESPACE, timer_id.as_bytes())
}
fn id_claim_key(tx: &AuthorityTransaction, timer_id: &str) -> io::Result<EntityKey> {
    global_key(tx, TIMER_ID_CLAIM_NAMESPACE, timer_id.as_bytes())
}
fn count_key(tx: &AuthorityTransaction, namespace: &str) -> io::Result<EntityKey> {
    owner_key(tx, namespace, COUNT_KEY)
}

fn descending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[!byte, 0]);
    }
    output.push(u8::MAX);
}
fn ascending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[byte, u8::MAX]);
    }
    output.push(0);
}

fn status_index_key(
    tx: &AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<EntityKey> {
    let status = text_field(document, "status")?;
    let created_at = text_field(document, "created_at")?;
    let timer_id = text_field(document, "id")?;
    let mut raw = Vec::with_capacity((created_at.len() + timer_id.len()) * 2 + 3);
    raw.push(u8::from(status != "active"));
    descending_text(&mut raw, created_at);
    descending_text(&mut raw, timer_id);
    owner_key(tx, TIMER_STATUS_CREATED_INDEX_NAMESPACE, &raw)
}
fn conversation_index_key(
    tx: &AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<EntityKey> {
    let conv_id = text_field(document, "conv_id")?;
    let mut raw = Vec::with_capacity(conv_id.len() + 2);
    raw.extend_from_slice(
        &u16::try_from(conv_id.len())
            .map_err(|_| invalid_input("timer conversation ID is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(conv_id.as_bytes());
    raw.extend_from_slice(text_field(document, "id")?.as_bytes());
    owner_key(tx, TIMER_CONVERSATION_INDEX_NAMESPACE, &raw)
}
fn global_active_index_key(
    tx: &AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<EntityKey> {
    let mut raw = Vec::new();
    ascending_text(&mut raw, text_field(document, "created_at")?);
    raw.extend_from_slice(&owner_field(document)?.to_be_bytes());
    ascending_text(&mut raw, text_field(document, "id")?);
    global_key(tx, TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE, &raw)
}
fn identity_value(document: &Map<String, Value>) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({
        "owner_user_id": owner_field(document)?,
        "id": text_field(document, "id")?,
    }))
    .map_err(|_| invalid_data("timer identity cannot be encoded"))
}
fn text_field<'a>(document: &'a Map<String, Value>, name: &str) -> io::Result<&'a str> {
    document
        .get(name)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("timer text field is malformed"))
}
fn owner_field(document: &Map<String, Value>) -> io::Result<u64> {
    document
        .get("user_id")
        .and_then(Value::as_u64)
        .filter(|owner| *owner > 0)
        .ok_or_else(|| invalid_data("timer owner is malformed"))
}

fn poll_prefix(conv_id: &str, timer_id: &str) -> io::Result<Vec<u8>> {
    let mut raw = Vec::with_capacity(conv_id.len() + timer_id.len() + 4);
    raw.extend_from_slice(
        &u16::try_from(conv_id.len())
            .map_err(|_| invalid_input("timer conversation ID is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(conv_id.as_bytes());
    raw.extend_from_slice(
        &u16::try_from(timer_id.len())
            .map_err(|_| invalid_input("timer ID is too long"))?
            .to_be_bytes(),
    );
    raw.extend_from_slice(timer_id.as_bytes());
    Ok(raw)
}
fn poll_document_key(
    tx: &AuthorityTransaction,
    conv_id: &str,
    timer_id: &str,
    poll_number: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_prefix(conv_id, timer_id)?;
    raw.extend_from_slice(&poll_number.to_be_bytes());
    owner_key(tx, TIMER_POLL_DOCUMENT_NAMESPACE, &raw)
}
fn poll_time_key(
    tx: &AuthorityTransaction,
    conv_id: &str,
    timer_id: &str,
    poll_time: &str,
    poll_number: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_prefix(conv_id, timer_id)?;
    descending_text(&mut raw, poll_time);
    raw.extend_from_slice(&(!poll_number).to_be_bytes());
    owner_key(tx, TIMER_POLL_TIME_INDEX_NAMESPACE, &raw)
}
fn poll_claim_key(
    tx: &AuthorityTransaction,
    conv_id: &str,
    timer_id: &str,
    poll_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = poll_prefix(conv_id, timer_id)?;
    raw.extend_from_slice(poll_id.as_bytes());
    owner_key(tx, TIMER_POLL_ID_CLAIM_NAMESPACE, &raw)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimerRestoreRequest {
    document: Map<String, Value>,
    updated_at_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimerGetRequest {
    id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimerScanRequest {
    cursor: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollRestoreRequest {
    timer_id: String,
    id: u64,
    poll: Map<String, Value>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollGetRequest {
    timer_id: String,
    id: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollScanRequest {
    timer_id: String,
    cursor: Option<u64>,
}

fn validate_timer_document(document: &Map<String, Value>, owner: u64) -> io::Result<()> {
    let timer_id = document
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_input("timer document is malformed"))?;
    if timer_id.is_empty()
        || timer_id.chars().count() > MAX_TIMER_ID_CHARACTERS
        || owner_field(document)? != owner
        || text_field(document, "conv_id")?.is_empty()
        || text_field(document, "status")?.is_empty()
        || document
            .get("tools_config")
            .is_none_or(|value| !value.is_object())
        || [
            "poll_interval",
            "max_polls",
            "poll_count",
            "promotion_streak",
            "fallback_streak",
        ]
        .iter()
        .any(|field| document.get(*field).and_then(Value::as_i64).is_none())
    {
        return Err(invalid_input("invalid timer snapshot metadata"));
    }
    if serde_json::to_vec(document)
        .map_err(|_| invalid_input("timer document cannot be encoded"))?
        .len()
        > MAX_TIMER_DOCUMENT_BYTES
    {
        return Err(invalid_input("timer document exceeds its byte bound"));
    }
    Ok(())
}

fn read_stored_document(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    timer_id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &document_key(tx, timer_id)?,
        LOGICAL_NAMESPACE,
        timer_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_TIMER_DOCUMENT_BYTES,
    )?
    .map(|raw| {
        serde_json::from_slice::<Value>(&raw)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("timer document is malformed"))
    })
    .transpose()
}

fn read_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
) -> io::Result<u64> {
    match database.entity_get(tx, &count_key(tx, namespace)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => {
            Ok(u64::from_le_bytes(raw.try_into().expect("length checked")))
        }
        Some(_) => Err(invalid_data("timer count is malformed")),
    }
}
fn write_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    namespace: &str,
    count: u64,
) -> io::Result<()> {
    database.entity_put(tx, count_key(tx, namespace)?, count.to_le_bytes().to_vec())
}
fn write_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let identity = identity_value(document)?;
    database.entity_put(tx, status_index_key(tx, document)?, identity.clone())?;
    database.entity_put(tx, conversation_index_key(tx, document)?, identity.clone())?;
    if text_field(document, "status")? == "active" {
        database.entity_put(tx, global_active_index_key(tx, document)?, identity)?;
    }
    Ok(())
}

fn restore_timer(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
    expected_uuid: uuid::Uuid,
) -> io::Result<bool> {
    let request: TimerRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid timer restore request"))?;
    if request.updated_at_ms == 0 {
        return Err(invalid_input("invalid timer snapshot metadata"));
    }
    let owner = tx.owner_user_id();
    validate_timer_document(&request.document, owner)?;
    let timer_id = request.document["id"]
        .as_str()
        .expect("validated")
        .to_owned();
    let encoded_document = serde_json::to_vec(&request.document)
        .map_err(|_| invalid_input("timer document cannot be encoded"))?;
    match database.entity_get(tx, &id_claim_key(tx, &timer_id)?)? {
        Some(claim) => {
            // Insert-or-match against the exact stored claim and document; a
            // divergent occupant fails closed instead of overwriting.
            if claim != identity_value(&request.document)?
                || read_stored_document(database, tx, &timer_id)?.as_ref()
                    != Some(&request.document)
            {
                return Err(snapshot_conflict());
            }
            return Ok(false);
        }
        None => {
            if database
                .entity_get(tx, &document_key(tx, &timer_id)?)?
                .is_some()
            {
                return Err(invalid_data("timer document has no ID claim"));
            }
        }
    }
    match crate::migration::restore_document(
        database,
        tx,
        expected_uuid,
        SnapshotDocument {
            key: document_key(tx, &timer_id)?,
            namespace: LOGICAL_NAMESPACE.into(),
            logical_key: timer_id.clone(),
            version: 1,
            updated_at_ms: request.updated_at_ms,
            value_json: encoded_document.clone(),
            blob_owner_user_id: TENANT_GLOBAL_OWNER_ID,
        },
    )? {
        SnapshotRestoreOutcome::Inserted => {}
        SnapshotRestoreOutcome::AlreadyMatches => {
            return Err(invalid_data("timer document exists without its ID claim"));
        }
    }
    write_indexes(database, tx, &request.document)?;
    database.entity_put(
        tx,
        id_claim_key(tx, &timer_id)?,
        identity_value(&request.document)?,
    )?;
    let active = u64::from(text_field(&request.document, "status")? == "active");
    let total = read_count(database, tx, TIMER_TOTAL_COUNT_NAMESPACE)?;
    let active_count = read_count(database, tx, TIMER_ACTIVE_COUNT_NAMESPACE)?;
    let next_total = total
        .checked_add(1)
        .ok_or_else(|| invalid_data("timer total count overflows"))?;
    let next_active = active_count
        .checked_add(active)
        .ok_or_else(|| invalid_data("timer active count overflows"))?;
    write_count(database, tx, TIMER_TOTAL_COUNT_NAMESPACE, next_total)?;
    write_count(database, tx, TIMER_ACTIVE_COUNT_NAMESPACE, next_active)?;
    // Read back through the decode path before publication.
    if read_stored_document(database, tx, &timer_id)?.as_ref() != Some(&request.document) {
        return Err(invalid_data("timer snapshot readback differs"));
    }
    Ok(true)
}

fn get_timer(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: TimerGetRequest =
        serde_json::from_value(payload).map_err(|_| invalid_input("invalid timer get request"))?;
    let Some(claim) = database.entity_get(tx, &id_claim_key(tx, &request.id)?)? else {
        return Ok(Value::Null);
    };
    let claimed_owner = serde_json::from_slice::<Value>(&claim)
        .ok()
        .and_then(|value| value.get("owner_user_id").and_then(Value::as_u64))
        .ok_or_else(|| invalid_data("timer ID claim is malformed"))?;
    if claimed_owner != tx.owner_user_id() {
        return Ok(Value::Null);
    }
    let Some(document) = read_stored_document(database, tx, &request.id)? else {
        return Err(invalid_data("timer ID claim target is missing"));
    };
    let snapshot = versioned_document::read_snapshot(
        database,
        tx,
        &document_key(tx, &request.id)?,
        LOGICAL_NAMESPACE,
        &request.id,
    )?
    .ok_or_else(|| invalid_data("timer ID claim target is missing"))?;
    Ok(json!({"document": document, "updated_at_ms": snapshot.updated_at_ms}))
}

fn scan_timers(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: TimerScanRequest =
        serde_json::from_value(payload).map_err(|_| invalid_input("invalid timer scan request"))?;
    let mut suffix = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("timer:")
            .ok_or_else(|| invalid_input("invalid timer catalog cursor"))?;
        if encoded.is_empty() {
            return Err(invalid_input("invalid timer catalog cursor"));
        }
        suffix = encoded.as_bytes().to_vec();
        suffix.push(0);
    }
    let start = global_key(tx, TIMER_ID_CLAIM_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        TIMER_ID_CLAIM_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, raw) in &page {
        let identity: Value = decode_claim(raw)?;
        let owner = identity
            .get("owner_user_id")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("timer catalog claim is malformed"))?;
        let timer_id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("timer catalog key is malformed"))?;
        if identity.get("id").and_then(Value::as_str) != Some(timer_id) {
            return Err(invalid_data("timer catalog claim differs"));
        }
        if owner == tx.owner_user_id() {
            rows.push(json!({"id": timer_id}));
        }
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("timer catalog page is malformed"))?;
        Some(format!("timer:{last}"))
    } else {
        None
    };
    if cursor.is_none() {
        // On a full pass the owner-scoped catalog must agree with the
        // restored counters exactly.
        if rows.len() as u64 != read_count(database, tx, TIMER_TOTAL_COUNT_NAMESPACE)? {
            return Err(invalid_data("timer total count and catalog differ"));
        }
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}
fn decode_claim(raw: &[u8]) -> io::Result<Value> {
    serde_json::from_slice(raw).map_err(|_| invalid_data("timer ID claim is malformed"))
}

fn restore_poll(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: PollRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid timer poll restore request"))?;
    if request.id == 0
        || request.timer_id.is_empty()
        || request.timer_id.chars().count() > MAX_TIMER_ID_CHARACTERS
    {
        return Err(invalid_input("invalid timer poll snapshot metadata"));
    }
    // The poll log is observability under its owning timer; a poll without a
    // restored timer fails closed instead of orphaning observability rows.
    let Some(timer) = read_stored_document(database, tx, &request.timer_id)? else {
        return Err(invalid_data("timer poll references a missing timer"));
    };
    if owner_field(&timer)? != tx.owner_user_id() {
        return Err(invalid_data("timer poll references a missing timer"));
    }
    let conv_id = text_field(&timer, "conv_id")?.to_owned();
    let poll_time = request
        .poll
        .get("poll_time")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_input("invalid timer poll snapshot metadata"))?;
    if poll_time.is_empty() || poll_time.chars().count() > 64 {
        return Err(invalid_input("invalid timer poll snapshot metadata"));
    }
    let poll_logical_id = request
        .poll
        .get("poll_id")
        .and_then(Value::as_str)
        .unwrap_or("");
    if poll_logical_id.chars().count() > 80 {
        return Err(invalid_input("invalid timer poll snapshot metadata"));
    }
    let mut document = request.poll.clone();
    document.insert("id".to_owned(), json!(request.id));
    document.insert("timer_id".to_owned(), json!(request.timer_id));
    let encoded =
        serde_json::to_vec(&document).map_err(|_| invalid_input("timer poll cannot be encoded"))?;
    if encoded.len() > MAX_TIMER_POLL_DOCUMENT_BYTES {
        return Err(invalid_input("timer poll document exceeds its byte bound"));
    }
    if let Some(existing) = database.entity_get(
        tx,
        &poll_document_key(tx, &conv_id, &request.timer_id, request.id)?,
    )? {
        if existing != encoded {
            return Err(snapshot_conflict());
        }
        return Ok(false);
    }
    database.entity_put(
        tx,
        poll_document_key(tx, &conv_id, &request.timer_id, request.id)?,
        encoded.clone(),
    )?;
    database.entity_put(
        tx,
        poll_time_key(tx, &conv_id, &request.timer_id, poll_time, request.id)?,
        request.id.to_le_bytes().to_vec(),
    )?;
    if !poll_logical_id.is_empty() {
        database.entity_put(
            tx,
            poll_claim_key(tx, &conv_id, &request.timer_id, poll_logical_id)?,
            request.id.to_le_bytes().to_vec(),
        )?;
    }
    // Advance the global poll sequence to the greatest restored ID so later
    // live polls never reuse a restored sequence number.
    let sequence_key = global_key(tx, TIMER_POLL_SEQUENCE_NAMESPACE, POLL_SEQUENCE_KEY)?;
    let current = match database.entity_get(tx, &sequence_key)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().expect("length checked")),
        Some(_) => return Err(invalid_data("timer poll sequence is malformed")),
    };
    if request.id > current {
        database.entity_put(tx, sequence_key, request.id.to_le_bytes().to_vec())?;
    }
    if database
        .entity_get(
            tx,
            &poll_document_key(tx, &conv_id, &request.timer_id, request.id)?,
        )?
        .as_deref()
        != Some(encoded.as_slice())
    {
        return Err(invalid_data("timer poll readback differs"));
    }
    Ok(true)
}

fn get_poll(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: PollGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid timer poll get request"))?;
    let Some(timer) = read_stored_document(database, tx, &request.timer_id)? else {
        return Ok(Value::Null);
    };
    if owner_field(&timer)? != tx.owner_user_id() {
        return Ok(Value::Null);
    }
    let conv_id = text_field(&timer, "conv_id")?;
    let Some(raw) = database.entity_get(
        tx,
        &poll_document_key(tx, conv_id, &request.timer_id, request.id)?,
    )?
    else {
        return Ok(Value::Null);
    };
    let document: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("timer poll document is malformed"))?;
    Ok(json!({"timer_id": request.timer_id, "id": request.id, "poll": document}))
}

fn scan_polls(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: PollScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid timer poll scan request"))?;
    if request.timer_id.is_empty() {
        return Err(invalid_input("invalid timer poll scan request"));
    }
    let Some(timer) = read_stored_document(database, tx, &request.timer_id)? else {
        return Ok(json!({"rows": [], "cursor": Value::Null}));
    };
    if owner_field(&timer)? != tx.owner_user_id() {
        return Ok(json!({"rows": [], "cursor": Value::Null}));
    }
    let conv_id = text_field(&timer, "conv_id")?;
    let mut prefix = poll_prefix(conv_id, &request.timer_id)?;
    if let Some(cursor) = request.cursor {
        prefix.extend_from_slice(&cursor.to_be_bytes());
        prefix.push(0);
    }
    let start = owner_key(tx, TIMER_POLL_DOCUMENT_NAMESPACE, &prefix)?;
    let end = owner_key(
        tx,
        TIMER_POLL_DOCUMENT_NAMESPACE,
        &successor(&poll_prefix(conv_id, &request.timer_id)?)?,
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (_, raw) in &page {
        let document: Value = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("timer poll document is malformed"))?;
        let id = document
            .get("id")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("timer poll document is malformed"))?;
        if document.get("timer_id").and_then(Value::as_str) != Some(request.timer_id.as_str()) {
            return Err(invalid_data("timer poll catalog crosses timers"));
        }
        rows.push(json!({"id": id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_u64))
            .ok_or_else(|| invalid_data("timer poll catalog page is malformed"))?;
        Some(json!(last))
    } else {
        None
    };
    Ok(json!({"rows": rows, "cursor": cursor}))
}
fn successor(prefix: &[u8]) -> io::Result<Vec<u8>> {
    let mut raw = prefix.to_vec();
    let index = raw
        .iter()
        .rposition(|byte| *byte != 255)
        .ok_or_else(|| invalid_data("timer poll prefix has no successor"))?;
    raw[index] += 1;
    raw.truncate(index + 1);
    Ok(raw)
}

pub(crate) fn execute(
    database: &mut AuthorityDatabase,
    tenant: u64,
    owner: u64,
    operation: &str,
    payload: Value,
) -> io::Result<Value> {
    let expected_uuid = database.authority_uuid();
    let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.timer.restore" => {
            let inserted = restore_timer(database, &mut transaction, payload, expected_uuid)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.timer.get" => get_timer(database, &mut transaction, payload),
        "migration.timer.scan" => scan_timers(database, &mut transaction, payload),
        "migration.timer.poll.restore" => {
            let inserted = restore_poll(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.timer.poll.get" => get_poll(database, &mut transaction, payload),
        "migration.timer.poll.scan" => scan_polls(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown timer migration operation")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::vfs::{DeterministicVfs, Vfs};
    use std::path::Path;
    use std::sync::Arc;

    fn prepared() -> (Arc<DeterministicVfs>, AuthorityDatabase) {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        (vfs, database)
    }

    fn document(id: &str, status: &str, poll_count: i64) -> Value {
        json!({
            "document": {
                "id": id,
                "user_id": 11,
                "conv_id": "conv-1",
                "check_instruction": "check",
                "continuation_message": "continue",
                "poll_interval": 60,
                "max_polls": 120,
                "poll_count": poll_count,
                "status": status,
                "tools_config": {},
                "created_at": "2026-09-01T00:00:00",
                "updated_at": "2026-09-01T00:00:00",
                "promotion_streak": 0,
                "fallback_streak": 0,
            },
            "updated_at_ms": 5_000,
        })
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let payload = document("t-1", "active", 7);
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.timer.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(&mut database, 7, 11, "migration.timer.restore", payload).unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.timer.get",
            json!({"id": "t-1"}),
        )
        .unwrap();
        assert_eq!(get["document"]["poll_count"], 7);
        assert_eq!(get["document"]["status"], "active");
        let divergent = document("t-1", "active", 8);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.timer.restore",
            divergent.clone()
        )
        .is_err());
    }

    #[test]
    fn counts_and_indexes_rebuilt_atomically() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.timer.restore",
            document("t-1", "active", 0),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.timer.restore",
            document("t-2", "completed", 3),
        )
        .unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            read_count(&database, &mut transaction, TIMER_TOTAL_COUNT_NAMESPACE).unwrap(),
            2
        );
        assert_eq!(
            read_count(&database, &mut transaction, TIMER_ACTIVE_COUNT_NAMESPACE).unwrap(),
            1
        );
        let doc = json!({
            "id": "t-1", "user_id": 11, "conv_id": "conv-1", "status": "active",
            "created_at": "2026-09-01T00:00:00",
        });
        let doc_map = doc.as_object().unwrap().clone();
        let status_key = status_index_key(&transaction, &doc_map).unwrap();
        assert!(database
            .entity_get(&mut transaction, &status_key)
            .unwrap()
            .is_some());
        let conversation_key = conversation_index_key(&transaction, &doc_map).unwrap();
        assert!(database
            .entity_get(&mut transaction, &conversation_key)
            .unwrap()
            .is_some());
        let active_key = global_active_index_key(&transaction, &doc_map).unwrap();
        assert!(database
            .entity_get(&mut transaction, &active_key)
            .unwrap()
            .is_some());
    }

    #[test]
    fn poll_restore_preserves_ids_and_advances_sequence() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.timer.restore",
            document("t-1", "active", 2),
        )
        .unwrap();
        let poll = json!({
            "timer_id": "t-1",
            "id": 41,
            "poll": {
                "poll_time": "2026-09-02T00:00:00",
                "decision": "wait",
                "reason": "",
                "poll_id": "logical-1",
            },
        });
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.timer.poll.restore",
                poll.clone()
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(&mut database, 7, 11, "migration.timer.poll.restore", poll).unwrap()
                ["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.timer.poll.get",
            json!({"timer_id": "t-1", "id": 41}),
        )
        .unwrap();
        assert_eq!(get["poll"]["decision"], "wait");
        assert_eq!(get["poll"]["id"], 41);
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let sequence_key = global_key(
            &transaction,
            TIMER_POLL_SEQUENCE_NAMESPACE,
            POLL_SEQUENCE_KEY,
        )
        .unwrap();
        let raw = database
            .entity_get(&mut transaction, &sequence_key)
            .unwrap()
            .unwrap();
        assert_eq!(u64::from_le_bytes(raw.try_into().unwrap()), 41);
    }

    #[test]
    fn scan_enumerates_catalogs_and_agrees_with_counts() {
        let (_vfs, mut database) = prepared();
        for id in ["t-1", "t-2", "t-3"] {
            execute(
                &mut database,
                7,
                11,
                "migration.timer.restore",
                document(id, "active", 0),
            )
            .unwrap();
        }
        execute(
            &mut database,
            7,
            11,
            "migration.timer.poll.restore",
            json!({"timer_id": "t-1", "id": 9, "poll": {"poll_time": "2026-09-02T00:00:00"}}),
        )
        .unwrap();
        let page = execute(
            &mut database,
            7,
            11,
            "migration.timer.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.timer.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
        let polls = execute(
            &mut database,
            7,
            11,
            "migration.timer.poll.scan",
            json!({"timer_id": "t-1", "cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(polls["rows"], json!([{"id": 9}]));
    }

    #[test]
    fn invalid_metadata_fails_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"document": {"id": "", "user_id": 11}, "updated_at_ms": 1}),
            json!({"document": {"id": "t"}, "updated_at_ms": 1}),
            json!({"document": {"id": "t", "user_id": 11, "conv_id": "c", "status": "active", "tools_config": {}, "poll_interval": 1, "max_polls": 1, "poll_count": 0, "promotion_streak": 0, "fallback_streak": 0}, "updated_at_ms": 0}),
        ] {
            assert!(execute(&mut database, 7, 11, "migration.timer.restore", payload).is_err());
        }
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.timer.poll.restore",
            json!({"timer_id": "missing", "id": 1, "poll": {"poll_time": "x"}})
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.timer.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
