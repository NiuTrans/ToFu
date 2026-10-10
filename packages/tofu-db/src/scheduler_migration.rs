//! Offline scheduled-task and proactive-poll-log snapshot restoration for the
//! exclusive operator importer.
//!
//! The source keeps one row per task in `storage_scheduled_tasks` and one
//! append-only observability row per poll in `storage_proactive_poll_log`.
//! The native authority stores a blob-capable versioned task document, a
//! tenant-global ID claim, an owner-local unique system-key claim, owner-local
//! and global created/enabled-created indexes, an owner task count, immutable
//! poll documents keyed by a global sequence with a per-task time index, and
//! the global poll sequence. Live creation derives claims and counters
//! itself, so replaying through it cannot preserve the exact source counters
//! or poll IDs and is not insert-or-match idempotent. This adapter restores
//! the document, both claims, all four indexes, and the count in the same
//! authority transaction; poll rows restore with their exact source poll ID,
//! advancing the global poll sequence to the greatest restored ID so later
//! live polls never collide. Insert-or-match fails closed on a divergent
//! occupant, get reconstructs the exact source snapshot, and scan enumerates
//! the owner-scoped catalogs independently, cross-checking the task catalog
//! against the restored count. Entry points: the
//! `migration.scheduler.task.*` and `migration.scheduler.poll.*` operations
//! dispatched from `migration_session`.

use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::io;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::TENANT_GLOBAL_OWNER_ID;
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_SCHEDULER_POLL_DOCUMENT_BYTES,
    MAX_SCHEDULER_TASK_DOCUMENT_BYTES, MAX_SCHEDULER_TASK_ID_CHARACTERS,
    MIGRATION_SESSION_MAX_PAGE_ROWS, SCHEDULER_POLL_DOCUMENT_NAMESPACE,
    SCHEDULER_POLL_SEQUENCE_NAMESPACE, SCHEDULER_POLL_TIME_INDEX_NAMESPACE,
    SCHEDULER_TASK_COUNT_NAMESPACE, SCHEDULER_TASK_CREATED_INDEX_NAMESPACE,
    SCHEDULER_TASK_DOCUMENT_NAMESPACE, SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE,
    SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
    SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE, SCHEDULER_TASK_ID_CLAIM_NAMESPACE,
    SCHEDULER_TASK_SYSTEM_KEY_CLAIM_NAMESPACE,
};
use crate::versioned_document::{self, SnapshotDocument, SnapshotRestoreOutcome};

const LOGICAL_NAMESPACE: &str = "scheduled_tasks";
const COUNT_KEY: &[u8] = b"count";
const POLL_SEQUENCE_KEY: &[u8] = b"sequence";

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}
fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}
fn snapshot_conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "scheduler snapshot differs")
}

fn owner_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), tx.owner_user_id(), namespace, raw)
}
fn global_key(tx: &AuthorityTransaction, namespace: &str, raw: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tx.tenant_id(), TENANT_GLOBAL_OWNER_ID, namespace, raw)
}
fn task_key(tx: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    global_key(tx, SCHEDULER_TASK_DOCUMENT_NAMESPACE, task_id.as_bytes())
}
fn claim_key(tx: &AuthorityTransaction, task_id: &str) -> io::Result<EntityKey> {
    global_key(tx, SCHEDULER_TASK_ID_CLAIM_NAMESPACE, task_id.as_bytes())
}
fn count_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    owner_key(tx, SCHEDULER_TASK_COUNT_NAMESPACE, COUNT_KEY)
}
fn system_key_key(tx: &AuthorityTransaction, system_key: &str) -> io::Result<EntityKey> {
    owner_key(
        tx,
        SCHEDULER_TASK_SYSTEM_KEY_CLAIM_NAMESPACE,
        system_key.as_bytes(),
    )
}

fn descending_text(output: &mut Vec<u8>, value: &str) {
    for byte in value.bytes() {
        output.extend_from_slice(&[!byte, 0]);
    }
    output.push(u8::MAX);
}
fn index_key(
    tx: &AuthorityTransaction,
    namespace: &str,
    created_at: &str,
    task_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((created_at.len() + task_id.len()) * 2 + 2);
    descending_text(&mut raw, created_at);
    descending_text(&mut raw, task_id);
    owner_key(tx, namespace, &raw)
}
fn global_index_key(
    tx: &AuthorityTransaction,
    namespace: &str,
    created_at: &str,
    owner_user_id: u64,
    task_id: &str,
) -> io::Result<EntityKey> {
    let mut raw = Vec::with_capacity((created_at.len() + task_id.len()) * 2 + 10);
    descending_text(&mut raw, created_at);
    raw.extend_from_slice(&owner_user_id.to_be_bytes());
    descending_text(&mut raw, task_id);
    global_key(tx, namespace, &raw)
}
fn index_value(task_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"id": task_id}))
        .map_err(|_| invalid_data("scheduler task index cannot be encoded"))
}
fn global_index_value(owner_user_id: u64, task_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"owner_user_id": owner_user_id, "id": task_id}))
        .map_err(|_| invalid_data("global scheduler task index cannot be encoded"))
}
fn claim_value(owner: u64, task_id: &str) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({"owner_user_id": owner, "id": task_id}))
        .map_err(|_| invalid_data("scheduler task claim cannot be encoded"))
}

fn poll_task_prefix(task_id: &str) -> io::Result<Vec<u8>> {
    let mut prefix = Vec::with_capacity(task_id.len() + 2);
    prefix.extend_from_slice(
        &u16::try_from(task_id.len())
            .map_err(|_| invalid_input("scheduler task ID is too long"))?
            .to_be_bytes(),
    );
    prefix.extend_from_slice(task_id.as_bytes());
    Ok(prefix)
}
fn poll_document_key(
    tx: &AuthorityTransaction,
    task_id: &str,
    poll_id: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_task_prefix(task_id)?;
    raw.extend_from_slice(&poll_id.to_be_bytes());
    owner_key(tx, SCHEDULER_POLL_DOCUMENT_NAMESPACE, &raw)
}
fn poll_index_key(
    tx: &AuthorityTransaction,
    task_id: &str,
    poll_time: &str,
    poll_id: u64,
) -> io::Result<EntityKey> {
    let mut raw = poll_task_prefix(task_id)?;
    descending_text(&mut raw, poll_time);
    raw.extend_from_slice(&(!poll_id).to_be_bytes());
    owner_key(tx, SCHEDULER_POLL_TIME_INDEX_NAMESPACE, &raw)
}
fn poll_sequence_key(tx: &AuthorityTransaction) -> io::Result<EntityKey> {
    global_key(tx, SCHEDULER_POLL_SEQUENCE_NAMESPACE, POLL_SEQUENCE_KEY)
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskRestoreRequest {
    document: Map<String, Value>,
    updated_at_ms: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskGetRequest {
    id: String,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TaskScanRequest {
    cursor: Option<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollRestoreRequest {
    task_id: String,
    id: u64,
    poll: Map<String, Value>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollGetRequest {
    task_id: String,
    id: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PollScanRequest {
    task_id: String,
    cursor: Option<u64>,
}

fn validate_task_document(document: &Map<String, Value>, owner: u64) -> io::Result<()> {
    let task_id = document
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_input("invalid scheduler snapshot metadata"))?;
    let system_key = document
        .get("system_key")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_input("invalid scheduler snapshot metadata"))?;
    let created_at = document
        .get("created_at")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_input("invalid scheduler snapshot metadata"))?;
    if task_id.is_empty()
        || task_id.chars().count() > MAX_SCHEDULER_TASK_ID_CHARACTERS
        || system_key.chars().count()
            > crate::generated_tofudb_ir::MAX_SCHEDULER_SYSTEM_KEY_CHARACTERS
        || document.get("user_id").and_then(Value::as_u64) != Some(owner)
        || document.get("enabled").and_then(Value::as_u64).is_none()
        || created_at.is_empty()
    {
        return Err(invalid_input("invalid scheduler snapshot metadata"));
    }
    if serde_json::to_vec(document)
        .map_err(|_| invalid_input("scheduler task document cannot be encoded"))?
        .len()
        > MAX_SCHEDULER_TASK_DOCUMENT_BYTES
    {
        return Err(invalid_input(
            "scheduler task document exceeds its byte bound",
        ));
    }
    Ok(())
}

fn read_stored_document(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    versioned_document::get_value_with_blob_owner_bounded(
        database,
        tx,
        &task_key(tx, task_id)?,
        LOGICAL_NAMESPACE,
        task_id,
        TENANT_GLOBAL_OWNER_ID,
        MAX_SCHEDULER_TASK_DOCUMENT_BYTES,
    )?
    .map(|raw| {
        serde_json::from_slice::<Value>(&raw)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("scheduler task document is malformed"))
    })
    .transpose()
}

fn read_count(database: &AuthorityDatabase, tx: &mut AuthorityTransaction) -> io::Result<u64> {
    match database.entity_get(tx, &count_key(tx)?)? {
        None => Ok(0),
        Some(raw) if raw.len() == 8 => {
            Ok(u64::from_le_bytes(raw.try_into().expect("length checked")))
        }
        Some(_) => Err(invalid_data("scheduler task count is malformed")),
    }
}
fn write_count(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    count: u64,
) -> io::Result<()> {
    database.entity_put(tx, count_key(tx)?, count.to_le_bytes().to_vec())
}

fn write_indexes(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let owner = tx.owner_user_id();
    let task_id = document["id"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler task ID is malformed"))?;
    let created_at = document["created_at"]
        .as_str()
        .ok_or_else(|| invalid_data("scheduler created_at is malformed"))?;
    let value = index_value(task_id)?;
    let global_value = global_index_value(owner, task_id)?;
    database.entity_put(
        tx,
        index_key(
            tx,
            SCHEDULER_TASK_CREATED_INDEX_NAMESPACE,
            created_at,
            task_id,
        )?,
        value.clone(),
    )?;
    database.entity_put(
        tx,
        global_index_key(
            tx,
            SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
            created_at,
            owner,
            task_id,
        )?,
        global_value.clone(),
    )?;
    if document["enabled"].as_u64() == Some(1) {
        database.entity_put(
            tx,
            index_key(
                tx,
                SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE,
                created_at,
                task_id,
            )?,
            value,
        )?;
        database.entity_put(
            tx,
            global_index_key(
                tx,
                SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE,
                created_at,
                owner,
                task_id,
            )?,
            global_value,
        )?;
    }
    Ok(())
}

fn restore_task(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
    expected_uuid: uuid::Uuid,
) -> io::Result<bool> {
    let request: TaskRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid scheduler restore request"))?;
    if request.updated_at_ms == 0 {
        return Err(invalid_input("invalid scheduler snapshot metadata"));
    }
    let owner = tx.owner_user_id();
    validate_task_document(&request.document, owner)?;
    let task_id = request.document["id"]
        .as_str()
        .expect("validated")
        .to_owned();
    let system_key = request.document["system_key"]
        .as_str()
        .expect("validated")
        .to_owned();
    let encoded_document = serde_json::to_vec(&request.document)
        .map_err(|_| invalid_input("scheduler task document cannot be encoded"))?;
    match database.entity_get(tx, &claim_key(tx, &task_id)?)? {
        Some(claim) => {
            // Insert-or-match against the exact stored claim and document; a
            // divergent occupant fails closed instead of overwriting.
            if claim != claim_value(owner, &task_id)?
                || read_stored_document(database, tx, &task_id)?.as_ref() != Some(&request.document)
            {
                return Err(snapshot_conflict());
            }
            return Ok(false);
        }
        None => {
            if database.entity_get(tx, &task_key(tx, &task_id)?)?.is_some() {
                return Err(invalid_data(
                    "scheduler task document has no identity claim",
                ));
            }
        }
    }
    // A non-empty system key is unique per owner; fail closed on collision.
    if !system_key.is_empty()
        && database
            .entity_get(tx, &system_key_key(tx, &system_key)?)?
            .is_some()
    {
        return Err(snapshot_conflict());
    }
    let count = read_count(database, tx)?;
    let next = count
        .checked_add(1)
        .ok_or_else(|| invalid_data("scheduler task count overflows"))?;
    match crate::migration::restore_document(
        database,
        tx,
        expected_uuid,
        SnapshotDocument {
            key: task_key(tx, &task_id)?,
            namespace: LOGICAL_NAMESPACE.into(),
            logical_key: task_id.clone(),
            version: 1,
            updated_at_ms: request.updated_at_ms,
            value_json: encoded_document.clone(),
            blob_owner_user_id: TENANT_GLOBAL_OWNER_ID,
        },
    )? {
        SnapshotRestoreOutcome::Inserted => {}
        SnapshotRestoreOutcome::AlreadyMatches => {
            return Err(invalid_data(
                "scheduler task document exists without its identity claim",
            ));
        }
    }
    write_indexes(database, tx, &request.document)?;
    database.entity_put(tx, claim_key(tx, &task_id)?, claim_value(owner, &task_id)?)?;
    if !system_key.is_empty() {
        database.entity_put(tx, system_key_key(tx, &system_key)?, index_value(&task_id)?)?;
    }
    write_count(database, tx, next)?;
    if read_stored_document(database, tx, &task_id)?.as_ref() != Some(&request.document) {
        return Err(invalid_data("scheduler snapshot readback differs"));
    }
    Ok(true)
}

fn get_task(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: TaskGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid scheduler get request"))?;
    let Some(claim) = database.entity_get(tx, &claim_key(tx, &request.id)?)? else {
        return Ok(Value::Null);
    };
    let claimed_owner = serde_json::from_slice::<Value>(&claim)
        .ok()
        .and_then(|value| value.get("owner_user_id").and_then(Value::as_u64))
        .ok_or_else(|| invalid_data("scheduler task identity claim is malformed"))?;
    if claimed_owner != tx.owner_user_id() {
        return Ok(Value::Null);
    }
    let Some(document) = read_stored_document(database, tx, &request.id)? else {
        return Err(invalid_data(
            "scheduler task identity claim target is missing",
        ));
    };
    let snapshot = versioned_document::read_snapshot(
        database,
        tx,
        &task_key(tx, &request.id)?,
        LOGICAL_NAMESPACE,
        &request.id,
    )?
    .ok_or_else(|| invalid_data("scheduler task identity claim target is missing"))?;
    Ok(json!({"document": document, "updated_at_ms": snapshot.updated_at_ms}))
}

fn scan_tasks(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: TaskScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid scheduler scan request"))?;
    let mut suffix = Vec::new();
    if let Some(cursor) = request.cursor.as_deref() {
        let encoded = cursor
            .strip_prefix("scheduler_task:")
            .ok_or_else(|| invalid_input("invalid scheduler catalog cursor"))?;
        if encoded.is_empty() {
            return Err(invalid_input("invalid scheduler catalog cursor"));
        }
        suffix = encoded.as_bytes().to_vec();
        suffix.push(0);
    }
    let start = global_key(tx, SCHEDULER_TASK_ID_CLAIM_NAMESPACE, &suffix)?;
    let (_, end) = EntityKey::prefix_range(
        tx.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        SCHEDULER_TASK_ID_CLAIM_NAMESPACE,
        b"",
    )?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (physical, raw) in &page {
        let identity: Value = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("scheduler task identity claim is malformed"))?;
        let owner = identity
            .get("owner_user_id")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("scheduler catalog claim is malformed"))?;
        let task_id = std::str::from_utf8(physical.key_bytes())
            .map_err(|_| invalid_data("scheduler catalog key is malformed"))?;
        if identity.get("id").and_then(Value::as_str) != Some(task_id) {
            return Err(invalid_data("scheduler catalog claim differs"));
        }
        if owner == tx.owner_user_id() {
            rows.push(json!({"id": task_id}));
        }
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_str))
            .ok_or_else(|| invalid_data("scheduler catalog page is malformed"))?;
        Some(format!("scheduler_task:{last}"))
    } else {
        None
    };
    // Count self-proof is only sound for a complete single-page catalog;
    // multi-page completeness is checked by the importer's row accounting.
    if request.cursor.is_none() && cursor.is_none() && rows.len() as u64 != read_count(database, tx)? {
        // On a full single-page pass the owner-scoped catalog must agree
        // with the restored task count exactly.
        return Err(invalid_data("scheduler task count and catalog differ"));
    }
    Ok(json!({"rows": rows, "cursor": cursor}))
}

fn restore_poll(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<bool> {
    let request: PollRestoreRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid scheduler poll restore request"))?;
    if request.id == 0
        || request.task_id.is_empty()
        || request.task_id.chars().count() > MAX_SCHEDULER_TASK_ID_CHARACTERS
    {
        return Err(invalid_input("invalid scheduler poll snapshot metadata"));
    }
    // The poll log is observability under its owning task; a poll without a
    // restored task fails closed instead of orphaning observability rows.
    let Some(task) = read_stored_document(database, tx, &request.task_id)? else {
        return Err(invalid_data("scheduler poll references a missing task"));
    };
    if task.get("user_id").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
        return Err(invalid_data("scheduler poll references a missing task"));
    }
    let poll_time = request
        .poll
        .get("poll_time")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_input("invalid scheduler poll snapshot metadata"))?;
    if poll_time.is_empty() || poll_time.chars().count() > 64 {
        return Err(invalid_input("invalid scheduler poll snapshot metadata"));
    }
    let mut document = request.poll.clone();
    document.insert("id".to_owned(), json!(request.id));
    document.insert("task_id".to_owned(), json!(request.task_id));
    let encoded = serde_json::to_vec(&document)
        .map_err(|_| invalid_input("scheduler poll cannot be encoded"))?;
    if encoded.len() > MAX_SCHEDULER_POLL_DOCUMENT_BYTES {
        return Err(invalid_input(
            "scheduler poll document exceeds its byte bound",
        ));
    }
    if let Some(existing) =
        database.entity_get(tx, &poll_document_key(tx, &request.task_id, request.id)?)?
    {
        if existing != encoded {
            return Err(snapshot_conflict());
        }
        return Ok(false);
    }
    database.entity_put(
        tx,
        poll_document_key(tx, &request.task_id, request.id)?,
        encoded.clone(),
    )?;
    database.entity_put(
        tx,
        poll_index_key(tx, &request.task_id, poll_time, request.id)?,
        request.id.to_le_bytes().to_vec(),
    )?;
    // Advance the global poll sequence to the greatest restored ID so later
    // live polls never reuse a restored sequence number.
    let current = match database.entity_get(tx, &poll_sequence_key(tx)?)? {
        None => 0,
        Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().expect("length checked")),
        Some(_) => return Err(invalid_data("scheduler poll sequence is malformed")),
    };
    if request.id > current {
        database.entity_put(
            tx,
            poll_sequence_key(tx)?,
            request.id.to_le_bytes().to_vec(),
        )?;
    }
    if database
        .entity_get(tx, &poll_document_key(tx, &request.task_id, request.id)?)?
        .as_deref()
        != Some(encoded.as_slice())
    {
        return Err(invalid_data("scheduler poll readback differs"));
    }
    Ok(true)
}

fn get_poll(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: PollGetRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid scheduler poll get request"))?;
    let Some(task) = read_stored_document(database, tx, &request.task_id)? else {
        return Ok(Value::Null);
    };
    if task.get("user_id").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
        return Ok(Value::Null);
    }
    let Some(raw) =
        database.entity_get(tx, &poll_document_key(tx, &request.task_id, request.id)?)?
    else {
        return Ok(Value::Null);
    };
    let document: Value = serde_json::from_slice(&raw)
        .map_err(|_| invalid_data("scheduler poll document is malformed"))?;
    Ok(json!({"task_id": request.task_id, "id": request.id, "poll": document}))
}

fn scan_polls(
    database: &AuthorityDatabase,
    tx: &mut AuthorityTransaction,
    payload: Value,
) -> io::Result<Value> {
    let request: PollScanRequest = serde_json::from_value(payload)
        .map_err(|_| invalid_input("invalid scheduler poll scan request"))?;
    if request.task_id.is_empty() {
        return Err(invalid_input("invalid scheduler poll scan request"));
    }
    let Some(task) = read_stored_document(database, tx, &request.task_id)? else {
        return Ok(json!({"rows": [], "cursor": Value::Null}));
    };
    if task.get("user_id").and_then(Value::as_u64) != Some(tx.owner_user_id()) {
        return Ok(json!({"rows": [], "cursor": Value::Null}));
    }
    let mut prefix = poll_task_prefix(&request.task_id)?;
    if let Some(cursor) = request.cursor {
        prefix.extend_from_slice(&cursor.to_be_bytes());
        prefix.push(0);
    }
    let start = owner_key(tx, SCHEDULER_POLL_DOCUMENT_NAMESPACE, &prefix)?;
    let mut end_prefix = poll_task_prefix(&request.task_id)?;
    let index = end_prefix
        .iter()
        .rposition(|byte| *byte != 255)
        .ok_or_else(|| invalid_data("scheduler poll prefix has no successor"))?;
    end_prefix[index] += 1;
    end_prefix.truncate(index + 1);
    let end = owner_key(tx, SCHEDULER_POLL_DOCUMENT_NAMESPACE, &end_prefix)?;
    let page = database.entity_scan(tx, &start, &end, MIGRATION_SESSION_MAX_PAGE_ROWS)?;
    let mut rows = Vec::with_capacity(page.len());
    for (_, raw) in &page {
        let document: Value = serde_json::from_slice(raw)
            .map_err(|_| invalid_data("scheduler poll document is malformed"))?;
        let id = document
            .get("id")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("scheduler poll document is malformed"))?;
        if document.get("task_id").and_then(Value::as_str) != Some(request.task_id.as_str()) {
            return Err(invalid_data("scheduler poll catalog crosses tasks"));
        }
        rows.push(json!({"id": id}));
    }
    let cursor = if page.len() == MIGRATION_SESSION_MAX_PAGE_ROWS {
        let last = rows
            .last()
            .and_then(|row| row.get("id").and_then(Value::as_u64))
            .ok_or_else(|| invalid_data("scheduler poll catalog page is malformed"))?;
        Some(json!(last))
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
    let expected_uuid = database.authority_uuid();
    let mut transaction = database.begin_with_identity_claim_scopes(tenant, owner)?;
    match operation {
        "migration.scheduler.task.restore" => {
            let inserted = restore_task(database, &mut transaction, payload, expected_uuid)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.scheduler.task.get" => get_task(database, &mut transaction, payload),
        "migration.scheduler.task.scan" => scan_tasks(database, &mut transaction, payload),
        "migration.scheduler.poll.restore" => {
            let inserted = restore_poll(database, &mut transaction, payload)?;
            if inserted {
                database.commit(transaction)?;
            }
            Ok(json!({"inserted": inserted}))
        }
        "migration.scheduler.poll.get" => get_poll(database, &mut transaction, payload),
        "migration.scheduler.poll.scan" => scan_polls(database, &mut transaction, payload),
        _ => Err(invalid_input("unknown scheduler migration operation")),
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

    fn document(id: &str, system_key: &str, enabled: u64) -> Value {
        json!({
            "document": {
                "id": id,
                "user_id": 11,
                "system_key": system_key,
                "name": format!("task-{id}"),
                "schedule": "* * * * *",
                "task_type": "command",
                "command": "echo ok",
                "enabled": enabled,
                "created_at": "2026-09-01T00:00:00",
                "updated_at": "2026-09-01T00:00:00",
            },
            "updated_at_ms": 5_000,
        })
    }

    #[test]
    fn restore_more_than_one_hundred_tasks_preserves_count_and_catalog() {
        let (_, mut database) = prepared();
        for index in 0..130 {
            let payload = document(&format!("many-{index:04}"), "", 1);
            assert_eq!(execute(&mut database, 7, 11, "migration.scheduler.task.restore", payload).unwrap()["inserted"], true);
        }
        let repeated = document("many-0129", "", 1);
        assert_eq!(execute(&mut database, 7, 11, "migration.scheduler.task.restore", repeated).unwrap()["inserted"], false);
        let mut cursor = Value::Null;
        let mut restored = Vec::new();
        loop {
            let page = execute(&mut database, 7, 11, "migration.scheduler.task.scan", json!({"cursor": cursor})).unwrap();
            restored.extend(page["rows"].as_array().unwrap().iter().cloned());
            cursor = page["cursor"].clone();
            if cursor.is_null() { break; }
        }
        assert_eq!(restored.len(), 130);
        assert_eq!(restored[129]["id"], "many-0129");
        let mut tx = database.begin(7, 11).unwrap();
        assert_eq!(read_count(&database, &mut tx).unwrap(), 130);
    }

    #[test]
    fn restore_preserves_exact_state_across_reopen_and_is_idempotent() {
        let (vfs, mut database) = prepared();
        let payload = document("s-1", "sys.key", 1);
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.scheduler.task.restore",
                payload.clone()
            )
            .unwrap()["inserted"],
            true
        );
        drop(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.scheduler.task.restore",
                payload
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.get",
            json!({"id": "s-1"}),
        )
        .unwrap();
        assert_eq!(get["document"]["system_key"], "sys.key");
        assert_eq!(get["document"]["enabled"], 1);
        let mut divergent = document("s-1", "sys.key", 0);
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.restore",
            divergent
        )
        .is_err());
        // A second task reusing the same non-empty system key fails closed.
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.restore",
            document("s-2", "sys.key", 1)
        )
        .is_err());
    }

    #[test]
    fn claims_indexes_and_count_rebuilt_atomically() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.restore",
            document("s-1", "sys.key", 1),
        )
        .unwrap();
        execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.restore",
            document("s-2", "", 0),
        )
        .unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(read_count(&database, &mut transaction).unwrap(), 2);
        let system_claim = system_key_key(&transaction, "sys.key").unwrap();
        let claim = database
            .entity_get(&mut transaction, &system_claim)
            .unwrap()
            .unwrap();
        let claim: Value = serde_json::from_slice(&claim).unwrap();
        assert_eq!(claim["id"], "s-1");
        // The enabled owner index holds only s-1; the created index holds both.
        let created = index_key(
            &transaction,
            SCHEDULER_TASK_CREATED_INDEX_NAMESPACE,
            "2026-09-01T00:00:00",
            "s-2",
        )
        .unwrap();
        assert!(database
            .entity_get(&mut transaction, &created)
            .unwrap()
            .is_some());
        let enabled = index_key(
            &transaction,
            SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE,
            "2026-09-01T00:00:00",
            "s-2",
        )
        .unwrap();
        assert!(database
            .entity_get(&mut transaction, &enabled)
            .unwrap()
            .is_none());
    }

    #[test]
    fn poll_restore_preserves_ids_and_advances_sequence() {
        let (_vfs, mut database) = prepared();
        execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.restore",
            document("s-1", "", 1),
        )
        .unwrap();
        let poll = json!({
            "task_id": "s-1",
            "id": 17,
            "poll": {"poll_time": "2026-09-02T00:00:00", "decision": "skip"},
        });
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.scheduler.poll.restore",
                poll.clone()
            )
            .unwrap()["inserted"],
            true
        );
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.scheduler.poll.restore",
                poll
            )
            .unwrap()["inserted"],
            false
        );
        let get = execute(
            &mut database,
            7,
            11,
            "migration.scheduler.poll.get",
            json!({"task_id": "s-1", "id": 17}),
        )
        .unwrap();
        assert_eq!(get["poll"]["decision"], "skip");
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let sequence_key = poll_sequence_key(&transaction).unwrap();
        let raw = database
            .entity_get(&mut transaction, &sequence_key)
            .unwrap()
            .unwrap();
        assert_eq!(u64::from_le_bytes(raw.try_into().unwrap()), 17);
    }

    #[test]
    fn scan_enumerates_catalogs_and_agrees_with_count() {
        let (_vfs, mut database) = prepared();
        for id in ["s-1", "s-2", "s-3"] {
            execute(
                &mut database,
                7,
                11,
                "migration.scheduler.task.restore",
                document(id, "", 1),
            )
            .unwrap();
        }
        execute(
            &mut database,
            7,
            11,
            "migration.scheduler.poll.restore",
            json!({"task_id": "s-1", "id": 3, "poll": {"poll_time": "2026-09-02T00:00:00"}}),
        )
        .unwrap();
        let page = execute(
            &mut database,
            7,
            11,
            "migration.scheduler.task.scan",
            json!({"cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(page["rows"].as_array().unwrap().len(), 3);
        assert_eq!(
            execute(
                &mut database,
                7,
                12,
                "migration.scheduler.task.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
        let polls = execute(
            &mut database,
            7,
            11,
            "migration.scheduler.poll.scan",
            json!({"task_id": "s-1", "cursor": Value::Null}),
        )
        .unwrap();
        assert_eq!(polls["rows"], json!([{"id": 3}]));
    }

    #[test]
    fn invalid_metadata_fails_before_any_write() {
        let (_vfs, mut database) = prepared();
        for payload in [
            json!({"document": {"id": "", "user_id": 11, "system_key": "", "created_at": "x", "enabled": 1}, "updated_at_ms": 1}),
            json!({"document": {"id": "s", "user_id": 12, "system_key": "", "created_at": "x", "enabled": 1}, "updated_at_ms": 1}),
            json!({"document": {"id": "s", "user_id": 11, "system_key": "", "created_at": "x", "enabled": 1}, "updated_at_ms": 0}),
        ] {
            assert!(execute(
                &mut database,
                7,
                11,
                "migration.scheduler.task.restore",
                payload
            )
            .is_err());
        }
        assert!(execute(
            &mut database,
            7,
            11,
            "migration.scheduler.poll.restore",
            json!({"task_id": "missing", "id": 1, "poll": {"poll_time": "x"}})
        )
        .is_err());
        assert_eq!(
            execute(
                &mut database,
                7,
                11,
                "migration.scheduler.task.scan",
                json!({"cursor": Value::Null})
            )
            .unwrap()["rows"],
            json!([])
        );
    }
}
