//! Owner-scoped Project Brain projection and immutable semantic event stream.
//!
//! A digest-keyed blob-capable projection carries bounded current state while
//! every accepted transition is chunked into the stream family in the same
//! authority transaction. Exact project identity remains inside the projection
//! so hash collisions fail closed. This module is the sole physical owner.

use std::collections::HashMap;
use std::io;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    MAX_PROJECT_BRAIN_ACTIVE_WORK, MAX_PROJECT_BRAIN_ARTIFACTS_PER_WORK,
    MAX_PROJECT_BRAIN_CHANGED_PATHS, MAX_PROJECT_BRAIN_CHARTER_DECISIONS,
    MAX_PROJECT_BRAIN_CHECKER_VERSIONS, MAX_PROJECT_BRAIN_CURSORS,
    MAX_PROJECT_BRAIN_DOCUMENT_BYTES, MAX_PROJECT_BRAIN_EVENT_BYTES, MAX_PROJECT_BRAIN_NARRATIVES,
    MAX_PROJECT_BRAIN_NARRATIVE_TEXT_BYTES, MAX_PROJECT_BRAIN_PROJECT_KEY_CHARACTERS,
    MAX_PROJECT_BRAIN_WATCH_ITEMS, MAX_PROJECT_BRAIN_WORK_HISTORY,
    PROJECT_BRAIN_CHECKPOINT_NAMESPACE, PROJECT_BRAIN_DOCUMENT_NAMESPACE,
    PROJECT_BRAIN_EVENT_CHECKPOINT_TAIL, PROJECT_BRAIN_EVENT_CHECKPOINT_THRESHOLD,
    PROJECT_BRAIN_EVENT_INDEX_NAMESPACE, PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE,
};
use crate::stream::{StreamEvent, StreamKey};
use crate::versioned_document::{self, PutRequest};

const LOGICAL_NAMESPACE: &str = "project_brain_projects";
const STREAM_DOMAIN: &str = "project_brain";
const EVENT_CHUNK_MAGIC: &[u8; 8] = b"TDBPBE01";
const EVENT_CHUNK_HEADER_BYTES: usize = 8 + 8 + 4 + 4 + 4 + 32;
const EVENT_CHUNK_BYTES: usize =
    crate::generated_tofudb_ir::MAX_STREAM_EVENT_BYTES - EVENT_CHUNK_HEADER_BYTES;
const PROJECT_BRAIN_VERSION: u64 = 1;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CommandAction {
    WorkStart,
    WorkRefine,
    WorkChange,
    WorkFinish,
    NarrativeAdd,
    CheckerRegister,
    CheckerResult,
    DecisionPromote,
    WatchAdd,
    WatchUpdate,
    WatchDelete,
    CursorPrepare,
    CursorConfirm,
}

pub(crate) struct CommandRequest {
    pub project_key: String,
    pub action: CommandAction,
    pub payload: Map<String, Value>,
    pub timestamp: u64,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn payload_too_large(message: &str) -> io::Error {
    io::Error::new(
        io::ErrorKind::OutOfMemory,
        format!("storage_payload_too_large: {message}"),
    )
}

pub(crate) fn normalize_project_key(raw: &str) -> Option<String> {
    if raw.is_empty() || raw.chars().count() > MAX_PROJECT_BRAIN_PROJECT_KEY_CHARACTERS {
        return None;
    }
    let trimmed = raw.trim().trim_end_matches(['/', '\\']);
    (!trimmed.is_empty()).then(|| trimmed.to_owned())
}

fn project_digest(project_key: &str) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"tofu-db:project-brain:v1\0");
    hasher.update(project_key.as_bytes());
    hasher.finalize().into()
}

fn document_key(transaction: &AuthorityTransaction, project_key: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_DOCUMENT_NAMESPACE,
        &project_digest(project_key),
    )
}

fn checkpoint_key(transaction: &AuthorityTransaction, project_key: &str) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_CHECKPOINT_NAMESPACE,
        &project_digest(project_key),
    )
}

fn event_index_key(
    transaction: &AuthorityTransaction,
    project_key: &str,
    logical_sequence: u64,
) -> io::Result<EntityKey> {
    let mut key = Vec::with_capacity(40);
    key.extend_from_slice(&project_digest(project_key));
    key.extend_from_slice(&logical_sequence.to_be_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_EVENT_INDEX_NAMESPACE,
        &key,
    )
}

fn stream_key(transaction: &AuthorityTransaction, project_key: &str) -> io::Result<StreamKey> {
    StreamKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        STREAM_DOMAIN,
        &project_digest(project_key),
    )
}

fn updated_index_key(
    transaction: &AuthorityTransaction,
    project_key: &str,
    updated_at: u64,
) -> io::Result<EntityKey> {
    let mut key = Vec::with_capacity(40);
    key.extend_from_slice(&(u64::MAX - updated_at).to_be_bytes());
    key.extend_from_slice(&project_digest(project_key));
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE,
        &key,
    )
}

fn updated_index_range(transaction: &AuthorityTransaction) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE,
        b"",
    )
}

fn empty_projection(owner_user_id: u64, project_key: &str) -> Map<String, Value> {
    json!({
        "version": PROJECT_BRAIN_VERSION,
        "ownerUserId": owner_user_id,
        "projectKey": project_key,
        "headSequence": 0,
        "checkpointSequence": 0,
        "workItems": [],
        "narratives": [],
        "charter": {"decisions": []},
        "checkers": [],
        "watch": [],
        "cursors": {},
    })
    .as_object()
    .unwrap()
    .clone()
}

fn load_projection(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
) -> io::Result<Map<String, Value>> {
    let key = document_key(transaction, project_key)?;
    let Some(raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &key,
        LOGICAL_NAMESPACE,
        project_key,
        transaction.owner_user_id(),
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?
    else {
        return Ok(empty_projection(transaction.owner_user_id(), project_key));
    };
    parse_stored_projection(&raw, transaction, project_key)
}

fn parse_stored_projection(
    raw: &[u8],
    transaction: &AuthorityTransaction,
    project_key: &str,
) -> io::Result<Map<String, Value>> {
    let mut projection = serde_json::from_slice::<Value>(raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("Project Brain projection is invalid"))?;
    if projection.get("version").and_then(Value::as_u64) != Some(PROJECT_BRAIN_VERSION)
        || projection.get("ownerUserId").and_then(Value::as_u64)
            != Some(transaction.owner_user_id())
        || projection.get("projectKey").and_then(Value::as_str) != Some(project_key)
    {
        return Err(invalid_data("Project Brain projection ownership mismatch"));
    }
    projection.remove("attention");
    Ok(projection)
}

fn save_projection(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    projection: &Map<String, Value>,
    timestamp: u64,
) -> io::Result<()> {
    let previous_updated_at = projection.get("_updatedAt").and_then(Value::as_u64);
    let mut stored_projection = projection.clone();
    stored_projection.insert("_updatedAt".to_owned(), Value::from(timestamp));
    let value_json = serde_json::to_vec(&Value::Object(stored_projection))
        .map_err(|_| invalid_data("Project Brain projection cannot be encoded"))?;
    if value_json.len() > MAX_PROJECT_BRAIN_DOCUMENT_BYTES {
        return Err(payload_too_large("Project Brain projection exceeds 8 MiB"));
    }
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: document_key(transaction, project_key)?,
            namespace: LOGICAL_NAMESPACE.to_owned(),
            logical_key: project_key.to_owned(),
            value_json,
            expected_version: None,
            updated_at_ms: timestamp.max(1),
        },
        transaction.owner_user_id(),
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?;
    if let Some(previous_updated_at) = previous_updated_at {
        let previous_key = updated_index_key(transaction, project_key, previous_updated_at)?;
        database.entity_delete(transaction, previous_key)?;
    }
    let current_key = updated_index_key(transaction, project_key, timestamp)?;
    database.entity_put(transaction, current_key, project_key.as_bytes().to_vec())?;
    Ok(())
}

fn save_checkpoint(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    projection: &Map<String, Value>,
    timestamp: u64,
) -> io::Result<()> {
    let value_json = serde_json::to_vec(&Value::Object(projection.clone()))
        .map_err(|_| invalid_data("Project Brain checkpoint cannot be encoded"))?;
    versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        PutRequest {
            key: checkpoint_key(transaction, project_key)?,
            namespace: "project_brain_checkpoints".to_owned(),
            logical_key: project_key.to_owned(),
            value_json,
            expected_version: None,
            updated_at_ms: timestamp.max(1),
        },
        transaction.owner_user_id(),
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?;
    Ok(())
}

fn required_text<'a>(
    object: &'a Map<String, Value>,
    key: &str,
    maximum: usize,
) -> io::Result<&'a str> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.chars().count() <= maximum)
        .ok_or_else(|| invalid_input("Invalid Project Brain text field"))
}

fn required_u64(
    object: &Map<String, Value>,
    key: &str,
    minimum: u64,
    maximum: u64,
) -> io::Result<u64> {
    object
        .get(key)
        .and_then(Value::as_u64)
        .filter(|value| (minimum..=maximum).contains(value))
        .ok_or_else(|| invalid_input("Invalid Project Brain integer field"))
}

fn string_list(
    value: Option<&Value>,
    maximum_items: usize,
    maximum_characters: usize,
) -> io::Result<Vec<Value>> {
    let values = value
        .and_then(Value::as_array)
        .filter(|values| values.len() <= maximum_items)
        .ok_or_else(|| invalid_input("Invalid Project Brain string list"))?;
    values
        .iter()
        .map(|value| {
            value
                .as_str()
                .filter(|value| !value.is_empty() && value.chars().count() <= maximum_characters)
                .map(|value| Value::String(value.to_owned()))
                .ok_or_else(|| invalid_input("Invalid Project Brain string-list item"))
        })
        .collect()
}

fn artifacts(value: Option<&Value>) -> io::Result<Vec<Value>> {
    let values = value
        .and_then(Value::as_array)
        .filter(|values| values.len() <= MAX_PROJECT_BRAIN_ARTIFACTS_PER_WORK)
        .ok_or_else(|| invalid_input("Invalid Project Brain artifacts"))?;
    values
        .iter()
        .map(|value| {
            let raw = value
                .as_object()
                .ok_or_else(|| invalid_input("Invalid Project Brain artifact"))?;
            let id = required_text(raw, "id", 256)?;
            let title = raw.get("title").and_then(Value::as_str).unwrap_or("");
            let format = raw.get("format").and_then(Value::as_str).unwrap_or("");
            let path = raw.get("path").and_then(Value::as_str).unwrap_or("");
            if title.chars().count() > 500
                || format.chars().count() > 128
                || path.chars().count() > 4096
            {
                return Err(invalid_input("Invalid Project Brain artifact"));
            }
            Ok(json!({"id": id, "title": title, "format": format, "path": path}))
        })
        .collect()
}

fn validated_work_item(value: &Map<String, Value>) -> io::Result<Value> {
    let task_id = required_text(value, "taskId", 256)?;
    let work_id = required_text(value, "id", 128)?;
    let expected_id = format!("pw_{:x}", Sha256::digest(task_id.as_bytes()));
    if work_id != &expected_id[..27] {
        return Err(invalid_input("Project work id is not deterministic"));
    }
    let trigger = required_text(value, "trigger", 32)?;
    if !matches!(trigger, "todo_write" | "file_write" | "isolated_workspace") {
        return Err(invalid_input("Invalid Project work trigger"));
    }
    if required_text(value, "status", 32)? != "active" {
        return Err(invalid_input("New Project work must be active"));
    }
    let result_summary = value
        .get("resultSummary")
        .and_then(Value::as_str)
        .filter(|value| value.chars().count() <= 4000)
        .ok_or_else(|| invalid_input("Invalid Project work resultSummary"))?;
    if value
        .get("finishedAt")
        .is_some_and(|value| !value.is_null())
    {
        return Err(invalid_input("New Project work cannot be terminal"));
    }
    let priority = required_u64(value, "_titlePriority", 1, 1000)?;
    let refined = value
        .get("_titleRefined")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    Ok(json!({
        "id": work_id,
        "taskId": task_id,
        "conversationId": required_text(value, "conversationId", 256)?,
        "title": required_text(value, "title", 500)?,
        "trigger": trigger,
        "status": "active",
        "changedPaths": string_list(value.get("changedPaths"), MAX_PROJECT_BRAIN_CHANGED_PATHS, 4096)?,
        "artifacts": artifacts(value.get("artifacts"))?,
        "resultSummary": result_summary,
        "startedAt": required_u64(value, "startedAt", 0, u64::MAX)?,
        "finishedAt": Value::Null,
        "_titlePriority": priority,
        "_titleRefined": refined,
    }))
}

fn validated_checker(value: &Map<String, Value>) -> io::Result<Value> {
    let argv = string_list(value.get("argv"), 32, 4096)?;
    let path_globs = string_list(value.get("pathGlobs"), 64, 4096)?;
    if argv.is_empty() || path_globs.is_empty() || !value["enabled"].is_boolean() {
        return Err(invalid_input("Invalid Checker definition"));
    }
    Ok(json!({
        "checkerId": required_text(value, "checkerId", 128)?,
        "version": required_u64(value, "version", 1, u64::MAX)?,
        "label": required_text(value, "label", 256)?,
        "argv": argv,
        "cwd": required_text(value, "cwd", 4096)?,
        "pathGlobs": path_globs,
        "timeoutMs": required_u64(value, "timeoutMs", 100, 3_600_000)?,
        "enabled": value["enabled"],
    }))
}

fn validated_watch_item(value: &Map<String, Value>) -> io::Result<Value> {
    let status = required_text(value, "status", 32)?;
    if !matches!(status, "active" | "resolved") {
        return Err(invalid_input("Invalid Project Watch status"));
    }
    let latest_result = match value.get("latestResult") {
        None | Some(Value::Null) => Value::Null,
        Some(Value::Object(latest)) => {
            let text = latest.get("text").and_then(Value::as_str).unwrap_or("");
            let trigger = latest.get("trigger").and_then(Value::as_str).unwrap_or("");
            if text.chars().count() > 4000 || trigger.chars().count() > 64 {
                return Err(invalid_input("Invalid Project Watch latestResult"));
            }
            json!({
                "text": text,
                "trigger": trigger,
                "timestamp": required_u64(latest, "timestamp", 0, u64::MAX)?,
            })
        }
        Some(_) => return Err(invalid_input("Invalid Project Watch latestResult")),
    };
    Ok(json!({
        "id": required_text(value, "id", 128)?,
        "kind": required_text(value, "kind", 64)?,
        "text": required_text(value, "text", 4000)?,
        "status": status,
        "sourceConversationId": value.get("sourceConversationId").and_then(Value::as_str).unwrap_or("").chars().take(256).collect::<String>(),
        "createdAt": required_u64(value, "createdAt", 0, u64::MAX)?,
        "updatedAt": required_u64(value, "updatedAt", 0, u64::MAX)?,
        "latestResult": latest_result,
    }))
}

fn checker_exists(projection: &Map<String, Value>, checker_id: &str, version: u64) -> bool {
    projection["checkers"].as_array().is_some_and(|checkers| {
        checkers.iter().any(|checker| {
            checker.get("checkerId").and_then(Value::as_str) == Some(checker_id)
                && checker.get("version").and_then(Value::as_u64) == Some(version)
        })
    })
}

fn watch_index(projection: &Map<String, Value>, item_id: &str) -> Option<usize> {
    projection["watch"].as_array().and_then(|items| {
        items
            .iter()
            .position(|item| item.get("id").and_then(Value::as_str) == Some(item_id))
    })
}

fn work_index(projection: &Map<String, Value>, work_id: &str) -> Option<usize> {
    projection
        .get("workItems")
        .and_then(Value::as_array)
        .and_then(|items| {
            items
                .iter()
                .position(|item| item.get("id").and_then(Value::as_str) == Some(work_id))
        })
}

fn bounded_work_items(items: &[Value]) -> Vec<Value> {
    let mut active = items
        .iter()
        .filter(|item| item.get("status").and_then(Value::as_str) == Some("active"))
        .cloned()
        .collect::<Vec<_>>();
    let mut terminal = items
        .iter()
        .filter(|item| {
            matches!(
                item.get("status").and_then(Value::as_str),
                Some("completed" | "failed" | "cancelled")
            )
        })
        .cloned()
        .collect::<Vec<_>>();
    active.sort_by_key(|item| item.get("startedAt").and_then(Value::as_u64).unwrap_or(0));
    terminal.sort_by_key(|item| item.get("finishedAt").and_then(Value::as_u64).unwrap_or(0));
    let active_start = active.len().saturating_sub(MAX_PROJECT_BRAIN_ACTIVE_WORK);
    let terminal_start = terminal
        .len()
        .saturating_sub(MAX_PROJECT_BRAIN_WORK_HISTORY);
    active[active_start..]
        .iter()
        .chain(&terminal[terminal_start..])
        .cloned()
        .collect()
}

fn bounded_utf8(value: &str, maximum_bytes: usize) -> String {
    let trimmed = value.trim();
    if trimmed.len() <= maximum_bytes {
        return trimmed.to_owned();
    }
    let mut end = maximum_bytes;
    while !trimmed.is_char_boundary(end) {
        end -= 1;
    }
    trimmed[..end].trim_end().to_owned()
}

fn append_narrative(
    projection: &mut Map<String, Value>,
    sequence: u64,
    kind: &str,
    text: &str,
    timestamp: u64,
    work_id: &str,
    conversation_id: &str,
) -> io::Result<()> {
    let kind = kind.chars().take(64).collect::<String>();
    let mut entry = json!({
        "sequence": sequence,
        "kind": if kind.is_empty() { "note" } else { &kind },
        "text": bounded_utf8(text, MAX_PROJECT_BRAIN_NARRATIVE_TEXT_BYTES),
        "timestamp": timestamp,
    });
    let object = entry.as_object_mut().unwrap();
    if !work_id.is_empty() {
        object.insert("workId".to_owned(), Value::String(work_id.to_owned()));
    }
    if !conversation_id.is_empty() {
        object.insert(
            "conversationId".to_owned(),
            Value::String(conversation_id.to_owned()),
        );
    }
    let narratives = projection
        .get_mut("narratives")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| invalid_data("Project Brain narratives are malformed"))?;
    narratives.push(entry);
    if narratives.len() > MAX_PROJECT_BRAIN_NARRATIVES {
        narratives.drain(..narratives.len() - MAX_PROJECT_BRAIN_NARRATIVES);
    }
    Ok(())
}

fn public_work_item(item: &Map<String, Value>) -> Value {
    let mut public = Map::new();
    for key in [
        "id",
        "taskId",
        "conversationId",
        "title",
        "trigger",
        "status",
        "changedPaths",
        "artifacts",
        "resultSummary",
        "startedAt",
        "finishedAt",
    ] {
        public.insert(
            key.to_owned(),
            item.get(key).cloned().unwrap_or(Value::Null),
        );
    }
    Value::Object(public)
}

fn public_work_items(projection: &Map<String, Value>, active_only: bool) -> io::Result<Vec<Value>> {
    Ok(projection
        .get("workItems")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid_data("Project Brain work items are malformed"))?
        .iter()
        .filter(|item| !active_only || item.get("status").and_then(Value::as_str) == Some("active"))
        .filter_map(Value::as_object)
        .map(public_work_item)
        .collect())
}

fn public_projection(projection: &Map<String, Value>) -> io::Result<Value> {
    let work_items = public_work_items(projection, false)?;
    Ok(json!({
        "version": projection.get("version").and_then(Value::as_u64).unwrap_or(PROJECT_BRAIN_VERSION),
        "ownerUserId": projection.get("ownerUserId").and_then(Value::as_u64).unwrap_or(0),
        "projectKey": projection.get("projectKey").and_then(Value::as_str).unwrap_or(""),
        "headSequence": projection.get("headSequence").and_then(Value::as_u64).unwrap_or(0),
        "checkpointSequence": projection.get("checkpointSequence").and_then(Value::as_u64).unwrap_or(0),
        "workItems": work_items,
        "narratives": projection.get("narratives").cloned().unwrap_or_else(|| json!([])),
        "charter": projection.get("charter").cloned().unwrap_or_else(|| json!({"decisions": []})),
        "checkers": projection.get("checkers").cloned().unwrap_or_else(|| json!([])),
        "watch": projection.get("watch").cloned().unwrap_or_else(|| json!([])),
    }))
}

fn append_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    event: &Value,
    logical_sequence: u64,
    timestamp: u64,
) -> io::Result<(u64, u64)> {
    let encoded = serde_json::to_vec(event)
        .map_err(|_| invalid_data("Project Brain event cannot be encoded"))?;
    if encoded.len() > MAX_PROJECT_BRAIN_EVENT_BYTES {
        return Err(payload_too_large("Project Brain event exceeds 8 MiB"));
    }
    let chunk_count = encoded.len().div_ceil(EVENT_CHUNK_BYTES).max(1);
    let chunk_count_u32 = u32::try_from(chunk_count)
        .map_err(|_| payload_too_large("Project Brain event has too many chunks"))?;
    let total_bytes = u32::try_from(encoded.len())
        .map_err(|_| payload_too_large("Project Brain event is too large"))?;
    let digest = blake3::hash(&encoded);
    let mut events = Vec::with_capacity(chunk_count);
    for (index, chunk) in encoded.chunks(EVENT_CHUNK_BYTES).enumerate() {
        let mut payload = Vec::with_capacity(EVENT_CHUNK_HEADER_BYTES + chunk.len());
        payload.extend_from_slice(EVENT_CHUNK_MAGIC);
        payload.extend_from_slice(&logical_sequence.to_be_bytes());
        payload.extend_from_slice(&(index as u32).to_be_bytes());
        payload.extend_from_slice(&chunk_count_u32.to_be_bytes());
        payload.extend_from_slice(&total_bytes.to_be_bytes());
        payload.extend_from_slice(digest.as_bytes());
        payload.extend_from_slice(chunk);
        events.push(StreamEvent::new(
            i64::try_from(timestamp)
                .map_err(|_| invalid_input("Project Brain timestamp overflow"))?,
            "semantic_event_chunk",
            payload,
        )?);
    }
    let key = stream_key(transaction, project_key)?;
    let next = database.transaction_stream_next_sequence(transaction, &key)?;
    let last = next
        .checked_add(chunk_count as u64 - 1)
        .ok_or_else(|| invalid_data("Project Brain physical sequence overflow"))?;
    database.stream_append(transaction, key, next, events)?;
    let mut position = Vec::with_capacity(16);
    position.extend_from_slice(&next.to_be_bytes());
    position.extend_from_slice(&last.to_be_bytes());
    database.entity_put(
        transaction,
        event_index_key(transaction, project_key, logical_sequence)?,
        position,
    )?;
    Ok((next, last))
}

fn decode_event_chunks(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    key: &StreamKey,
    first_physical_sequence: u64,
) -> io::Result<(Value, u64)> {
    let first_page =
        database.stream_read_in_transaction(transaction, key, first_physical_sequence, 1)?;
    let first = first_page
        .events
        .first()
        .ok_or_else(|| invalid_data("Project Brain event chunk is missing"))?;
    let header = first.event.payload.as_slice();
    if first.sequence != first_physical_sequence
        || first.event.event_type != "semantic_event_chunk"
        || header.len() < EVENT_CHUNK_HEADER_BYTES
        || &header[..8] != EVENT_CHUNK_MAGIC
    {
        return Err(invalid_data(
            "Project Brain event chunk header is malformed",
        ));
    }
    let logical_sequence = u64::from_be_bytes(header[8..16].try_into().unwrap());
    let first_chunk_index = u32::from_be_bytes(header[16..20].try_into().unwrap());
    let chunk_count = u32::from_be_bytes(header[20..24].try_into().unwrap()) as usize;
    let total_bytes = u32::from_be_bytes(header[24..28].try_into().unwrap()) as usize;
    let digest: [u8; 32] = header[28..60].try_into().unwrap();
    if first_chunk_index != 0
        || chunk_count == 0
        || chunk_count > MAX_PROJECT_BRAIN_EVENT_BYTES.div_ceil(EVENT_CHUNK_BYTES)
        || total_bytes > MAX_PROJECT_BRAIN_EVENT_BYTES
    {
        return Err(invalid_data(
            "Project Brain event chunk bounds are malformed",
        ));
    }
    let page = database.stream_read_in_transaction(
        transaction,
        key,
        first_physical_sequence,
        chunk_count,
    )?;
    if page.events.len() != chunk_count {
        return Err(invalid_data("Project Brain event chunks are incomplete"));
    }
    let mut encoded = Vec::with_capacity(total_bytes);
    for (index, chunk) in page.events.iter().enumerate() {
        let payload = chunk.event.payload.as_slice();
        if chunk.sequence != first_physical_sequence + index as u64
            || chunk.event.event_type != "semantic_event_chunk"
            || payload.len() < EVENT_CHUNK_HEADER_BYTES
            || &payload[..8] != EVENT_CHUNK_MAGIC
            || u64::from_be_bytes(payload[8..16].try_into().unwrap()) != logical_sequence
            || u32::from_be_bytes(payload[16..20].try_into().unwrap()) as usize != index
            || u32::from_be_bytes(payload[20..24].try_into().unwrap()) as usize != chunk_count
            || u32::from_be_bytes(payload[24..28].try_into().unwrap()) as usize != total_bytes
            || payload[28..60] != digest
        {
            return Err(invalid_data(
                "Project Brain event chunk sequence is malformed",
            ));
        }
        encoded.extend_from_slice(&payload[EVENT_CHUNK_HEADER_BYTES..]);
        if encoded.len() > total_bytes {
            return Err(invalid_data(
                "Project Brain event chunks exceed declared length",
            ));
        }
    }
    if encoded.len() != total_bytes || blake3::hash(&encoded).as_bytes() != &digest {
        return Err(invalid_data("Project Brain event chunk digest differs"));
    }
    let event: Value = serde_json::from_slice(&encoded)
        .map_err(|_| invalid_data("Project Brain event JSON is malformed"))?;
    let next = first_physical_sequence
        .checked_add(chunk_count as u64)
        .ok_or_else(|| invalid_data("Project Brain physical sequence overflow"))?;
    Ok((event, next))
}

fn install_checkpoint_if_due(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    projection: &mut Map<String, Value>,
    timestamp: u64,
) -> io::Result<()> {
    let head = projection
        .get("headSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Project Brain head sequence is malformed"))?;
    let checkpoint = projection
        .get("checkpointSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Project Brain checkpoint sequence is malformed"))?;
    if head.saturating_sub(checkpoint) < PROJECT_BRAIN_EVENT_CHECKPOINT_THRESHOLD {
        return Ok(());
    }
    let checkpoint_sequence = head
        .checked_add(1)
        .ok_or_else(|| invalid_data("Project Brain checkpoint sequence overflow"))?;
    projection.insert("headSequence".to_owned(), Value::from(checkpoint_sequence));
    projection.insert(
        "checkpointSequence".to_owned(),
        Value::from(checkpoint_sequence),
    );
    save_checkpoint(database, transaction, project_key, projection, timestamp)?;
    let snapshot_bytes = serde_json::to_vec(&Value::Object(projection.clone()))
        .map_err(|_| invalid_data("Project Brain checkpoint cannot be encoded"))?;
    let checkpoint_event = json!({
        "ownerUserId": transaction.owner_user_id(),
        "projectKey": project_key,
        "projectSequence": checkpoint_sequence,
        "kind": "projection_checkpoint",
        "timestamp": timestamp,
        "payload": {
            "snapshotDigest": blake3::hash(&snapshot_bytes).to_hex().to_string(),
            "snapshotBytes": snapshot_bytes.len(),
        },
    });
    append_event(
        database,
        transaction,
        project_key,
        &checkpoint_event,
        checkpoint_sequence,
        timestamp,
    )?;

    let retain_logical_sequence = checkpoint_sequence
        .checked_sub(PROJECT_BRAIN_EVENT_CHECKPOINT_TAIL)
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| invalid_data("Project Brain retained-tail sequence underflow"))?;
    let digest = project_digest(project_key);
    let index_start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_EVENT_INDEX_NAMESPACE,
        &digest,
    )?;
    let mut index_end_bytes = Vec::with_capacity(40);
    index_end_bytes.extend_from_slice(&digest);
    index_end_bytes.extend_from_slice(&retain_logical_sequence.to_be_bytes());
    let index_end = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_EVENT_INDEX_NAMESPACE,
        &index_end_bytes,
    )?;
    database.entity_retire_range(transaction, &index_start, &index_end)?;
    Ok(())
}

fn drain_checkpointed_stream_prefix(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    retain_physical_sequence: u64,
) -> io::Result<()> {
    let key = stream_key(transaction, project_key)?;
    let _progress = database.stream_retire_prefix(transaction, &key, retain_physical_sequence)?;
    Ok(())
}

fn continue_checkpoint_retirement(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    projection: &Map<String, Value>,
) -> io::Result<()> {
    let checkpoint_sequence = projection
        .get("checkpointSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Project Brain checkpoint sequence is malformed"))?;
    if checkpoint_sequence == 0 {
        return Ok(());
    }
    let retain_logical_sequence = checkpoint_sequence
        .checked_sub(PROJECT_BRAIN_EVENT_CHECKPOINT_TAIL)
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| invalid_data("Project Brain retained-tail sequence underflow"))?;
    let retained_position = database
        .entity_get(
            transaction,
            &event_index_key(transaction, project_key, retain_logical_sequence)?,
        )?
        .ok_or_else(|| invalid_data("Project Brain retained-tail position is missing"))?;
    if retained_position.len() != 16 {
        return Err(invalid_data("Project Brain event position is malformed"));
    }
    let retain_physical_sequence = u64::from_be_bytes(retained_position[..8].try_into().unwrap());
    drain_checkpointed_stream_prefix(database, transaction, project_key, retain_physical_sequence)
}

fn command_result(projection: &Map<String, Value>, event: Option<Value>) -> io::Result<Vec<u8>> {
    let head = projection
        .get("headSequence")
        .and_then(Value::as_u64)
        .unwrap_or(0);
    serde_json::to_vec(&json!({
        "ok": true,
        "event": event,
        "projection": public_projection(projection)?,
        "pushHint": {"type": "project_brain_changed", "projectSequence": head},
    }))
    .map_err(|_| invalid_data("Project Brain command response cannot be encoded"))
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
) -> io::Result<Vec<u8>> {
    let projection = load_projection(database, transaction, project_key)?;
    serde_json::to_vec(&public_projection(&projection)?)
        .map_err(|_| invalid_data("Project Brain projection response cannot be encoded"))
}

pub(crate) fn list_active(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let (start, end) = updated_index_range(transaction)?;
    let rows = database.entity_scan(transaction, &start, &end, 1000)?;
    let mut projects = Vec::new();
    for (_, raw_project_key) in rows {
        let project_key = std::str::from_utf8(&raw_project_key)
            .map_err(|_| invalid_data("Project Brain updated index is malformed"))?;
        let projection = load_projection(database, transaction, project_key)?;
        let work_items = public_work_items(&projection, true)?;
        if !work_items.is_empty() {
            projects.push(json!({"projectKey": project_key, "workItems": work_items}));
        }
    }
    serde_json::to_vec(&json!({"projects": projects}))
        .map_err(|_| invalid_data("Project Brain active-list response cannot be encoded"))
}

pub(crate) fn recovery_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let (start, end) = updated_index_range(transaction)?;
    let rows = database.entity_scan(transaction, &start, &end, 1000)?;
    let capped = rows.len() >= 1000;
    let mut projects = Vec::new();
    for (_, raw_project_key) in rows {
        let project_key = std::str::from_utf8(&raw_project_key)
            .map_err(|_| invalid_data("Project Brain updated index is malformed"))?;
        let projection = load_projection(database, transaction, project_key)?;
        let work_items = public_work_items(&projection, true)?;
        if !work_items.is_empty() {
            projects.push(json!({
                "ownerUserId": transaction.owner_user_id(),
                "projectKey": project_key,
                "workItems": work_items,
            }));
        }
    }
    serde_json::to_vec(&json!({"projects": projects, "capped": capped}))
        .map_err(|_| invalid_data("Project Brain recovery snapshot cannot be encoded"))
}

pub(crate) fn rebuild(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    timestamp: u64,
) -> io::Result<Vec<u8>> {
    let key = stream_key(transaction, project_key)?;
    let physical_end = database.transaction_stream_next_sequence(transaction, &key)?;
    let checkpoint_document = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &checkpoint_key(transaction, project_key)?,
        "project_brain_checkpoints",
        project_key,
        transaction.owner_user_id(),
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?;
    let (mut projection, mut physical_sequence, mut logical_sequence) =
        if let Some(raw_checkpoint) = checkpoint_document {
            let projection = serde_json::from_slice::<Value>(&raw_checkpoint)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("Project Brain checkpoint is invalid"))?;
            let checkpoint_sequence = projection
                .get("checkpointSequence")
                .and_then(Value::as_u64)
                .filter(|sequence| {
                    projection.get("headSequence").and_then(Value::as_u64) == Some(*sequence)
                })
                .ok_or_else(|| invalid_data("Project Brain checkpoint sequence is invalid"))?;
            if projection.get("ownerUserId").and_then(Value::as_u64)
                != Some(transaction.owner_user_id())
                || projection.get("projectKey").and_then(Value::as_str) != Some(project_key)
            {
                return Err(invalid_data("Project Brain checkpoint ownership mismatch"));
            }
            let checkpoint_position = database
                .entity_get(
                    transaction,
                    &event_index_key(transaction, project_key, checkpoint_sequence)?,
                )?
                .ok_or_else(|| invalid_data("Project Brain checkpoint position is missing"))?;
            if checkpoint_position.len() != 16 {
                return Err(invalid_data(
                    "Project Brain checkpoint position is malformed",
                ));
            }
            let checkpoint_last =
                u64::from_be_bytes(checkpoint_position[8..16].try_into().unwrap());
            (
                projection,
                checkpoint_last
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("Project Brain physical sequence overflow"))?,
                checkpoint_sequence
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("Project Brain logical sequence overflow"))?,
            )
        } else {
            if physical_end == 1 {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "Project Brain event stream not found",
                ));
            }
            (
                empty_projection(transaction.owner_user_id(), project_key),
                1,
                1,
            )
        };
    let mut replayed_events = 0_u64;
    while physical_sequence < physical_end {
        let (event, next_physical_sequence) =
            decode_event_chunks(database, transaction, &key, physical_sequence)?;
        if event.get("ownerUserId").and_then(Value::as_u64) != Some(transaction.owner_user_id())
            || event.get("projectKey").and_then(Value::as_str) != Some(project_key)
            || event.get("projectSequence").and_then(Value::as_u64) != Some(logical_sequence)
        {
            return Err(invalid_data("Project Brain event identity differs"));
        }
        fold_event(&mut projection, &event)?;
        physical_sequence = next_physical_sequence;
        logical_sequence = logical_sequence
            .checked_add(1)
            .ok_or_else(|| invalid_data("Project Brain logical sequence overflow"))?;
        replayed_events = replayed_events
            .checked_add(1)
            .ok_or_else(|| invalid_data("Project Brain replay count overflow"))?;
    }
    if physical_sequence != physical_end {
        return Err(invalid_data("Project Brain physical stream has a gap"));
    }
    save_projection(database, transaction, project_key, &projection, timestamp)?;
    serde_json::to_vec(&json!({
        "ok": true,
        "projectKey": project_key,
        "headSequence": projection["headSequence"],
        "checkpointSequence": projection["checkpointSequence"],
        "replayedEvents": replayed_events,
        "projection": public_projection(&projection)?,
    }))
    .map_err(|_| invalid_data("Project Brain rebuild response cannot be encoded"))
}

fn work_authoritative_key(item: &Map<String, Value>) -> (u64, u64, bool) {
    (
        item.get("finishedAt").and_then(Value::as_u64).unwrap_or(0),
        item.get("startedAt").and_then(Value::as_u64).unwrap_or(0),
        matches!(
            item.get("status").and_then(Value::as_str),
            Some("completed" | "failed" | "cancelled")
        ),
    )
}

fn merge_relinked_immutable_rows<K: Clone + Eq + std::hash::Hash>(
    destination_rows: Option<&Value>,
    source_rows: Option<&Value>,
    identity: impl Fn(&Map<String, Value>) -> K,
    maximum: usize,
    field: &str,
) -> io::Result<Vec<Value>> {
    let mut order: Vec<K> = Vec::new();
    let mut rows: HashMap<K, Map<String, Value>> = HashMap::new();
    for values in [destination_rows, source_rows].into_iter().flatten() {
        let Some(items) = values.as_array() else {
            continue;
        };
        for raw in items {
            let Some(item) = raw.as_object() else {
                continue;
            };
            let key = identity(item);
            match rows.get(&key) {
                Some(previous) if previous != item => {
                    return Err(conflict(&format!(
                        "Project Brain {field} identity conflict during relink"
                    )));
                }
                Some(_) => {}
                None => {
                    order.push(key.clone());
                    rows.insert(key, item.clone());
                }
            }
        }
    }
    if rows.len() > maximum {
        return Err(payload_too_large(&format!(
            "Project Brain {field} limit reached during relink"
        )));
    }
    Ok(order
        .into_iter()
        .map(|key| Value::Object(rows[&key].clone()))
        .collect())
}

fn merge_relinked_projections(
    owner_user_id: u64,
    project_key: &str,
    destination: &Map<String, Value>,
    source: &Map<String, Value>,
) -> io::Result<Map<String, Value>> {
    let mut merged = source.clone();
    for (key, value) in destination {
        merged.insert(key.clone(), value.clone());
    }
    merged.insert("version".to_owned(), Value::from(PROJECT_BRAIN_VERSION));
    merged.insert("ownerUserId".to_owned(), Value::from(owner_user_id));
    merged.insert("projectKey".to_owned(), Value::from(project_key));
    merged.insert("checkpointSequence".to_owned(), Value::from(0));

    let mut work_order: Vec<String> = Vec::new();
    let mut work_by_id: HashMap<String, Map<String, Value>> = HashMap::new();
    for projection in [destination, source] {
        let Some(items) = projection.get("workItems").and_then(Value::as_array) else {
            continue;
        };
        for raw in items {
            let Some(item) = raw.as_object() else {
                continue;
            };
            let work_id = item
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            if work_id.is_empty() {
                continue;
            }
            let Some(previous) = work_by_id.get(&work_id) else {
                work_order.push(work_id.clone());
                work_by_id.insert(work_id, item.clone());
                continue;
            };
            // Python max returns the first maximal candidate, so ties keep previous.
            let authoritative = if work_authoritative_key(item) > work_authoritative_key(previous)
            {
                item
            } else {
                previous
            };
            let mut combined = authoritative.clone();
            let mut changed_paths: Vec<Value> = Vec::new();
            for list in [previous.get("changedPaths"), item.get("changedPaths")]
                .into_iter()
                .flatten()
            {
                if let Some(values) = list.as_array() {
                    for value in values {
                        if !changed_paths.contains(value) {
                            changed_paths.push(value.clone());
                        }
                    }
                }
            }
            let changed_start = changed_paths
                .len()
                .saturating_sub(MAX_PROJECT_BRAIN_CHANGED_PATHS);
            combined.insert(
                "changedPaths".to_owned(),
                Value::Array(changed_paths[changed_start..].to_vec()),
            );
            let mut artifact_order: Vec<String> = Vec::new();
            let mut artifacts_by_key: HashMap<String, Map<String, Value>> = HashMap::new();
            for list in [previous.get("artifacts"), item.get("artifacts")]
                .into_iter()
                .flatten()
            {
                if let Some(values) = list.as_array() {
                    for artifact in values {
                        let Some(artifact) = artifact.as_object() else {
                            continue;
                        };
                        let key = artifact
                            .get("id")
                            .and_then(Value::as_str)
                            .or_else(|| artifact.get("path").and_then(Value::as_str))
                            .unwrap_or("")
                            .to_owned();
                        if key.is_empty() {
                            continue;
                        }
                        if !artifacts_by_key.contains_key(&key) {
                            artifact_order.push(key.clone());
                        }
                        artifacts_by_key.insert(key, artifact.clone());
                    }
                }
            }
            let artifact_values: Vec<Value> = artifact_order
                .iter()
                .map(|key| Value::Object(artifacts_by_key[key].clone()))
                .collect();
            let artifact_start = artifact_values
                .len()
                .saturating_sub(MAX_PROJECT_BRAIN_ARTIFACTS_PER_WORK);
            combined.insert(
                "artifacts".to_owned(),
                Value::Array(artifact_values[artifact_start..].to_vec()),
            );
            work_by_id.insert(work_id, combined);
        }
    }
    let work_values: Vec<Value> = work_order
        .iter()
        .map(|work_id| Value::Object(work_by_id[work_id].clone()))
        .collect();
    merged.insert(
        "workItems".to_owned(),
        Value::Array(bounded_work_items(&work_values)),
    );

    let mut narrative_order: Vec<(String, String, u64, String, String)> = Vec::new();
    let mut narratives: HashMap<(String, String, u64, String, String), Map<String, Value>> =
        HashMap::new();
    for projection in [destination, source] {
        let Some(items) = projection.get("narratives").and_then(Value::as_array) else {
            continue;
        };
        for raw in items {
            let Some(item) = raw.as_object() else {
                continue;
            };
            let key = (
                item.get("kind")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                item.get("text")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                item.get("timestamp").and_then(Value::as_u64).unwrap_or(0),
                item.get("workId")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
                item.get("conversationId")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            );
            if !narratives.contains_key(&key) {
                narrative_order.push(key.clone());
            }
            narratives.insert(key, item.clone());
        }
    }
    let mut narrative_rows: Vec<Map<String, Value>> = narrative_order
        .iter()
        .map(|key| narratives[key].clone())
        .collect();
    narrative_rows.sort_by_key(|item| {
        (
            item.get("timestamp").and_then(Value::as_u64).unwrap_or(0),
            item.get("sequence").and_then(Value::as_u64).unwrap_or(0),
        )
    });
    let narrative_start = narrative_rows
        .len()
        .saturating_sub(MAX_PROJECT_BRAIN_NARRATIVES);
    let mut narrative_rows = narrative_rows[narrative_start..].to_vec();
    let combined_head = (destination
        .get("headSequence")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        + source
            .get("headSequence")
            .and_then(Value::as_u64)
            .unwrap_or(0))
    .max(narrative_rows.len() as u64);
    let first_sequence = combined_head - narrative_rows.len() as u64 + 1;
    for (offset, item) in narrative_rows.iter_mut().enumerate() {
        item.insert(
            "sequence".to_owned(),
            Value::from(first_sequence + offset as u64),
        );
    }
    merged.insert(
        "narratives".to_owned(),
        Value::Array(narrative_rows.into_iter().map(Value::Object).collect()),
    );
    merged.insert("headSequence".to_owned(), Value::from(combined_head));

    merged.insert(
        "checkers".to_owned(),
        Value::Array(merge_relinked_immutable_rows(
            destination.get("checkers"),
            source.get("checkers"),
            |item| {
                (
                    item.get("checkerId")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                    item.get("version").and_then(Value::as_u64).unwrap_or(0),
                )
            },
            MAX_PROJECT_BRAIN_CHECKER_VERSIONS,
            "checkers",
        )?),
    );
    let destination_charter = destination
        .get("charter")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let source_charter = source
        .get("charter")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let decisions = merge_relinked_immutable_rows(
        destination_charter.get("decisions"),
        source_charter.get("decisions"),
        |item| {
            item.get("decisionId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned()
        },
        MAX_PROJECT_BRAIN_CHARTER_DECISIONS,
        "charter decisions",
    )?;
    let mut charter = source_charter;
    for (key, value) in destination_charter {
        charter.insert(key, value);
    }
    charter.insert("decisions".to_owned(), Value::Array(decisions));
    merged.insert("charter".to_owned(), Value::Object(charter));

    let mut watch_order: Vec<String> = Vec::new();
    let mut watch_by_id: HashMap<String, Map<String, Value>> = HashMap::new();
    for projection in [destination, source] {
        let Some(items) = projection.get("watch").and_then(Value::as_array) else {
            continue;
        };
        for raw in items {
            let Some(item) = raw.as_object() else {
                continue;
            };
            let item_id = item
                .get("id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
            let replace = match watch_by_id.get(&item_id) {
                None => true,
                Some(current) => {
                    item.get("updatedAt").and_then(Value::as_u64).unwrap_or(0)
                        >= current.get("updatedAt").and_then(Value::as_u64).unwrap_or(0)
                }
            };
            if replace {
                if !watch_by_id.contains_key(&item_id) {
                    watch_order.push(item_id.clone());
                }
                watch_by_id.insert(item_id, item.clone());
            }
        }
    }
    if watch_by_id.len() > MAX_PROJECT_BRAIN_WATCH_ITEMS {
        return Err(payload_too_large(
            "Project Brain watch limit reached during relink",
        ));
    }
    let mut watch_rows: Vec<Map<String, Value>> = watch_order
        .iter()
        .map(|item_id| watch_by_id[item_id].clone())
        .collect();
    watch_rows.sort_by_key(|item| item.get("updatedAt").and_then(Value::as_u64).unwrap_or(0));
    merged.insert(
        "watch".to_owned(),
        Value::Array(watch_rows.into_iter().map(Value::Object).collect()),
    );

    merged.insert("cursors".to_owned(), Value::Object(Map::new()));
    Ok(merged)
}

fn retire_scope_physical_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    project_key: &str,
    updated_at: u64,
) -> io::Result<()> {
    let stream = stream_key(transaction, project_key)?;
    let physical_end = database.transaction_stream_next_sequence(transaction, &stream)?;
    if physical_end > 1 {
        database.stream_retire_prefix(transaction, &stream, physical_end)?;
    }
    let digest = project_digest(project_key);
    let (index_start, index_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        PROJECT_BRAIN_EVENT_INDEX_NAMESPACE,
        &digest,
    )?;
    database.entity_retire_range(transaction, &index_start, &index_end)?;
    versioned_document::delete(
        database,
        transaction,
        document_key(transaction, project_key)?,
        LOGICAL_NAMESPACE,
        project_key,
        None,
    )?;
    let checkpoint_document_key = checkpoint_key(transaction, project_key)?;
    if database
        .entity_get(transaction, &checkpoint_document_key)?
        .is_some()
    {
        versioned_document::delete(
            database,
            transaction,
            checkpoint_document_key,
            "project_brain_checkpoints",
            project_key,
            None,
        )?;
    }
    database.entity_delete(
        transaction,
        updated_index_key(transaction, project_key, updated_at)?,
    )?;
    Ok(())
}

pub(crate) fn relink_scope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    old_project_key: &str,
    new_project_key: &str,
    timestamp: u64,
) -> io::Result<bool> {
    let old_project_key = old_project_key.trim_end_matches(['/', '\\']);
    let new_project_key = new_project_key.trim_end_matches(['/', '\\']);
    if old_project_key == new_project_key {
        // Distinct raw paths can collapse to one brain identity after the
        // trailing-separator strip; the projection already lives at the
        // destination key, so there is nothing to move.
        return Ok(true);
    }
    let owner_user_id = transaction.owner_user_id();
    let old_document_key = document_key(transaction, old_project_key)?;
    let Some(old_raw) = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &old_document_key,
        LOGICAL_NAMESPACE,
        old_project_key,
        owner_user_id,
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?
    else {
        return Ok(false);
    };
    let old_projection = parse_stored_projection(&old_raw, transaction, old_project_key)?;
    let old_updated = old_projection
        .get("_updatedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Project Brain projection timestamp is missing"))?;

    let new_document_key = document_key(transaction, new_project_key)?;
    let destination_raw = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &new_document_key,
        LOGICAL_NAMESPACE,
        new_project_key,
        owner_user_id,
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?;

    if let Some(destination_raw) = destination_raw {
        let destination =
            parse_stored_projection(&destination_raw, transaction, new_project_key)?;
        let destination_updated = destination
            .get("_updatedAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("Project Brain projection timestamp is missing"))?;
        let mut source = old_projection.clone();
        source.insert("projectKey".to_owned(), Value::from(new_project_key));
        let mut merged =
            merge_relinked_projections(owner_user_id, new_project_key, &destination, &source)?;
        let checkpoint_timestamp = old_updated.max(destination_updated).max(timestamp);
        let head = merged
            .get("headSequence")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("Project Brain head sequence is malformed"))?;
        let checkpoint_sequence = head
            .checked_add(1)
            .ok_or_else(|| invalid_data("Project Brain checkpoint sequence overflow"))?;
        merged.insert(
            "headSequence".to_owned(),
            Value::from(checkpoint_sequence),
        );
        merged.insert(
            "checkpointSequence".to_owned(),
            Value::from(checkpoint_sequence),
        );
        save_checkpoint(
            database,
            transaction,
            new_project_key,
            &merged,
            checkpoint_timestamp,
        )?;
        let snapshot_bytes = serde_json::to_vec(&Value::Object(merged.clone()))
            .map_err(|_| invalid_data("Project Brain checkpoint cannot be encoded"))?;
        let checkpoint_event = json!({
            "ownerUserId": owner_user_id,
            "projectKey": new_project_key,
            "projectSequence": checkpoint_sequence,
            "kind": "projection_checkpoint",
            "timestamp": checkpoint_timestamp,
            "payload": {
                "snapshotDigest": blake3::hash(&snapshot_bytes).to_hex().to_string(),
                "snapshotBytes": snapshot_bytes.len(),
            },
        });
        append_event(
            database,
            transaction,
            new_project_key,
            &checkpoint_event,
            checkpoint_sequence,
            checkpoint_timestamp,
        )?;
        if let Some(retain_logical_sequence) = checkpoint_sequence
            .checked_sub(PROJECT_BRAIN_EVENT_CHECKPOINT_TAIL)
            .and_then(|value| value.checked_add(1))
        {
            let digest = project_digest(new_project_key);
            let index_start = EntityKey::new(
                transaction.tenant_id(),
                owner_user_id,
                PROJECT_BRAIN_EVENT_INDEX_NAMESPACE,
                &digest,
            )?;
            let index_end = event_index_key(transaction, new_project_key, retain_logical_sequence)?;
            database.entity_retire_range(transaction, &index_start, &index_end)?;
        }
        save_projection(
            database,
            transaction,
            new_project_key,
            &merged,
            checkpoint_timestamp,
        )?;
        retire_scope_physical_state(database, transaction, old_project_key, old_updated)?;
        return Ok(true);
    }

    let old_stream = stream_key(transaction, old_project_key)?;
    let physical_end = database.transaction_stream_next_sequence(transaction, &old_stream)?;
    let old_digest = project_digest(old_project_key);
    let (index_start, index_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        owner_user_id,
        PROJECT_BRAIN_EVENT_INDEX_NAMESPACE,
        &old_digest,
    )?;
    let mut moved_events: Vec<(u64, Value)> = Vec::new();
    let first_index = database.entity_scan(transaction, &index_start, &index_end, 1)?;
    if let Some((_, position)) = first_index.first() {
        if position.len() != 16 {
            return Err(invalid_data("Project Brain event position is malformed"));
        }
        let mut physical_sequence = u64::from_be_bytes(position[..8].try_into().unwrap());
        while physical_sequence < physical_end {
            let (event, next_physical_sequence) = decode_event_chunks(
                database,
                transaction,
                &old_stream,
                physical_sequence,
            )?;
            if event.get("ownerUserId").and_then(Value::as_u64) != Some(owner_user_id)
                || event.get("projectKey").and_then(Value::as_str) != Some(old_project_key)
            {
                return Err(invalid_data("Project Brain event identity differs"));
            }
            let logical_sequence = event
                .get("projectSequence")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("Project Brain event sequence is malformed"))?;
            let mut moved = event
                .as_object()
                .cloned()
                .ok_or_else(|| invalid_data("Project Brain event is invalid"))?;
            moved.insert("projectKey".to_owned(), Value::from(new_project_key));
            moved_events.push((logical_sequence, Value::Object(moved)));
            physical_sequence = next_physical_sequence;
        }
        if physical_sequence != physical_end {
            return Err(invalid_data("Project Brain physical stream has a gap"));
        }
    }
    for (logical_sequence, event) in &moved_events {
        let event_timestamp = event
            .get("timestamp")
            .and_then(Value::as_u64)
            .unwrap_or(timestamp);
        append_event(
            database,
            transaction,
            new_project_key,
            event,
            *logical_sequence,
            event_timestamp,
        )?;
    }
    let old_checkpoint_key = checkpoint_key(transaction, old_project_key)?;
    let old_checkpoint = versioned_document::get_value_with_blob_owner_bounded(
        database,
        transaction,
        &old_checkpoint_key,
        "project_brain_checkpoints",
        old_project_key,
        owner_user_id,
        MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
    )?;
    if let Some(raw_checkpoint) = old_checkpoint {
        let mut checkpoint = serde_json::from_slice::<Value>(&raw_checkpoint)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("Project Brain checkpoint is invalid"))?;
        if checkpoint.get("ownerUserId").and_then(Value::as_u64) != Some(owner_user_id)
            || checkpoint.get("projectKey").and_then(Value::as_str) != Some(old_project_key)
        {
            return Err(invalid_data("Project Brain checkpoint ownership mismatch"));
        }
        checkpoint.insert("projectKey".to_owned(), Value::from(new_project_key));
        save_checkpoint(database, transaction, new_project_key, &checkpoint, timestamp)?;
    }
    retire_scope_physical_state(database, transaction, old_project_key, old_updated)?;
    let mut document = old_projection;
    document.insert("projectKey".to_owned(), Value::from(new_project_key));
    save_projection(
        database,
        transaction,
        new_project_key,
        &document,
        old_updated,
    )?;
    Ok(true)
}

fn cursor_prepare(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CommandRequest,
) -> io::Result<Vec<u8>> {
    let conversation_id = required_text(&request.payload, "conversation_id", 256)?;
    let mut projection = load_projection(database, transaction, &request.project_key)?;
    if projection["cursors"].get(conversation_id).is_none() {
        let sequence = projection["headSequence"]
            .as_u64()
            .and_then(|value| value.checked_add(1))
            .ok_or_else(|| invalid_data("Project Brain sequence overflow"))?;
        let event = json!({
            "ownerUserId": transaction.owner_user_id(),
            "projectKey": request.project_key,
            "projectSequence": sequence,
            "kind": "cursor_initialized",
            "timestamp": request.timestamp,
            "payload": {
                "conversationId": conversation_id,
                "deliveredSequence": sequence,
            },
        });
        fold_event(&mut projection, &event)?;
        append_event(
            database,
            transaction,
            &request.project_key,
            &event,
            sequence,
            request.timestamp,
        )?;
        install_checkpoint_if_due(
            database,
            transaction,
            &request.project_key,
            &mut projection,
            request.timestamp,
        )?;
        continue_checkpoint_retirement(database, transaction, &request.project_key, &projection)?;
        save_projection(
            database,
            transaction,
            &request.project_key,
            &projection,
            request.timestamp,
        )?;
        return serde_json::to_vec(&json!({
            "initialized": true,
            "entries": [],
            "fromSequence": sequence,
            "toSequence": sequence,
            "headSequence": sequence,
            "deliveryToken": "",
        }))
        .map_err(|_| invalid_data("Project Brain cursor response cannot be encoded"));
    }

    let delivered = projection["cursors"][conversation_id]["deliveredSequence"]
        .as_u64()
        .unwrap_or(0);
    let limit = match request.payload.get("limit") {
        None => 12,
        Some(_) => required_u64(&request.payload, "limit", 1, 12)? as usize,
    };
    let token_budget = match request.payload.get("token_budget") {
        None => 900,
        Some(_) => required_u64(&request.payload, "token_budget", 1, 900)? as usize,
    };
    let mut entries = Vec::new();
    let mut spent = 0_usize;
    for item in projection["narratives"].as_array().unwrap() {
        let sequence = item.get("sequence").and_then(Value::as_u64).unwrap_or(0);
        if sequence <= delivered {
            continue;
        }
        let cost = item
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or("")
            .len()
            .max(1)
            + 12;
        if !entries.is_empty() && spent + cost > token_budget {
            break;
        }
        if entries.is_empty() && cost > token_budget {
            return Err(invalid_data(
                "Stored Project narrative exceeds the delivery budget",
            ));
        }
        entries.push(item.clone());
        spent += cost;
        if entries.len() >= limit {
            break;
        }
    }
    let to_sequence = entries
        .last()
        .and_then(|item| item.get("sequence"))
        .and_then(Value::as_u64)
        .unwrap_or(delivered);
    let delivery_token = if entries.is_empty() {
        String::new()
    } else {
        format!(
            "{:x}",
            Sha256::digest(
                format!(
                    "{}\0{}\0{}\0{}\0{}",
                    transaction.owner_user_id(),
                    request.project_key,
                    conversation_id,
                    delivered,
                    to_sequence
                )
                .as_bytes()
            )
        )
    };
    serde_json::to_vec(&json!({
        "initialized": false,
        "entries": entries,
        "fromSequence": delivered,
        "toSequence": to_sequence,
        "headSequence": projection["headSequence"],
        "deliveryToken": delivery_token,
    }))
    .map_err(|_| invalid_data("Project Brain cursor response cannot be encoded"))
}

pub(crate) fn command(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CommandRequest,
) -> io::Result<Vec<u8>> {
    if request.action == CommandAction::CursorPrepare {
        return cursor_prepare(database, transaction, request);
    }
    let mut projection = load_projection(database, transaction, &request.project_key)?;
    let current_head = projection
        .get("headSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Project Brain head sequence is malformed"))?;
    let (kind, event_payload) = match request.action {
        CommandAction::WorkStart => {
            let work_item = request
                .payload
                .get("work_item")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("work_item must be an object"))?;
            let validated = validated_work_item(work_item)?;
            let work_id = validated.get("id").and_then(Value::as_str).unwrap();
            if let Some(index) = work_index(&projection, work_id) {
                let existing = &projection["workItems"][index];
                if existing.get("taskId") != validated.get("taskId")
                    || existing.get("conversationId") != validated.get("conversationId")
                {
                    return Err(conflict("Project work ownership is immutable"));
                }
                return command_result(&projection, None);
            }
            let active_count = projection["workItems"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|item| item.get("status").and_then(Value::as_str) == Some("active"))
                .count();
            if active_count >= MAX_PROJECT_BRAIN_ACTIVE_WORK {
                return Err(payload_too_large("Project active-work limit reached"));
            }
            ("work_started", json!({"workItem": validated}))
        }
        CommandAction::WorkRefine => {
            let work_id = required_text(&request.payload, "work_id", 128)?;
            let index = work_index(&projection, work_id).ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "Project work item not found")
            })?;
            let item = &projection["workItems"][index];
            let title = required_text(&request.payload, "title", 500)?.trim();
            let priority = required_u64(&request.payload, "title_priority", 1, 1000)?;
            if item.get("status").and_then(Value::as_str) != Some("active")
                || item.get("_titleRefined").and_then(Value::as_bool) == Some(true)
                || priority
                    <= item
                        .get("_titlePriority")
                        .and_then(Value::as_u64)
                        .unwrap_or(0)
            {
                return command_result(&projection, None);
            }
            (
                "work_title_refined",
                json!({"workId": work_id, "title": title, "titlePriority": priority}),
            )
        }
        CommandAction::WorkChange => {
            let work_id = required_text(&request.payload, "work_id", 128)?;
            if work_index(&projection, work_id).is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "Project work item not found",
                ));
            }
            let changed_paths = match request.payload.get("changed_paths") {
                Some(value) => string_list(Some(value), MAX_PROJECT_BRAIN_CHANGED_PATHS, 4096)?,
                None => Vec::new(),
            };
            let changed_artifacts = match request.payload.get("artifacts") {
                Some(value) => artifacts(Some(value))?,
                None => Vec::new(),
            };
            (
                "work_changed",
                json!({
                    "workId": work_id,
                    "changedPaths": changed_paths,
                    "artifacts": changed_artifacts,
                }),
            )
        }
        CommandAction::WorkFinish => {
            let work_id = required_text(&request.payload, "work_id", 128)?;
            let index = work_index(&projection, work_id).ok_or_else(|| {
                io::Error::new(io::ErrorKind::NotFound, "Project work item not found")
            })?;
            if matches!(
                projection["workItems"][index]
                    .get("status")
                    .and_then(Value::as_str),
                Some("completed" | "failed" | "cancelled")
            ) {
                return command_result(&projection, None);
            }
            let status = required_text(&request.payload, "status", 32)?;
            if !matches!(status, "completed" | "failed" | "cancelled") {
                return Err(invalid_input("Invalid Project work terminal status"));
            }
            let result_summary = request
                .payload
                .get("result_summary")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(4000)
                .collect::<String>();
            (
                "work_finished",
                json!({"workId": work_id, "status": status, "resultSummary": result_summary}),
            )
        }
        CommandAction::NarrativeAdd => {
            let text = required_text(&request.payload, "text", 720)?;
            let kind = required_text(&request.payload, "kind", 64)?;
            let work_id = request
                .payload
                .get("work_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(128)
                .collect::<String>();
            let conversation_id = request
                .payload
                .get("conversation_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(256)
                .collect::<String>();
            (
                "narrative_added",
                json!({
                    "narrativeKind": kind,
                    "text": bounded_utf8(text, MAX_PROJECT_BRAIN_NARRATIVE_TEXT_BYTES),
                    "workId": work_id,
                    "conversationId": conversation_id,
                }),
            )
        }
        CommandAction::CheckerRegister => {
            let definition = request
                .payload
                .get("definition")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("Checker definition must be an object"))?;
            let validated = validated_checker(definition)?;
            let checker_id = validated["checkerId"].as_str().unwrap();
            let version = validated["version"].as_u64().unwrap();
            if let Some(existing) = projection["checkers"]
                .as_array()
                .unwrap()
                .iter()
                .find(|item| {
                    item.get("checkerId").and_then(Value::as_str) == Some(checker_id)
                        && item.get("version").and_then(Value::as_u64) == Some(version)
                })
            {
                if existing != &validated {
                    return Err(conflict("Checker versions are immutable"));
                }
                return command_result(&projection, None);
            }
            if projection["checkers"].as_array().unwrap().len()
                >= MAX_PROJECT_BRAIN_CHECKER_VERSIONS
            {
                return Err(payload_too_large("Project checker-version limit reached"));
            }
            ("checker_registered", json!({"definition": validated}))
        }
        CommandAction::DecisionPromote => {
            let decision = request
                .payload
                .get("decision")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("Decision must be an object"))?;
            let checker_ref = decision
                .get("checkerRef")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("Decision checkerRef is required"))?;
            let checker_id = required_text(checker_ref, "id", 128)?;
            let version = required_u64(checker_ref, "version", 1, u64::MAX)?;
            if !checker_exists(&projection, checker_id, version) {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "Decision references an unknown checker version",
                ));
            }
            let decision_id = required_text(decision, "decisionId", 128)?;
            required_text(decision, "text", 4000)?;
            required_text(decision, "sourceConversationId", 256)?;
            required_text(decision, "sourceTurnId", 256)?;
            if !matches!(
                decision.get("latestVerification"),
                Some(Value::Null | Value::Object(_))
            ) {
                return Err(invalid_input(
                    "Decision latestVerification must be null or an object",
                ));
            }
            let decisions = projection["charter"]["decisions"].as_array().unwrap();
            if let Some(existing) = decisions
                .iter()
                .find(|item| item.get("decisionId").and_then(Value::as_str) == Some(decision_id))
            {
                if existing != &Value::Object(decision.clone()) {
                    return Err(conflict("Charter decisions are immutable"));
                }
                return command_result(&projection, None);
            }
            if decisions.len() >= MAX_PROJECT_BRAIN_CHARTER_DECISIONS {
                return Err(payload_too_large("Project Charter decision limit reached"));
            }
            (
                "decision_promoted",
                json!({"decision": Value::Object(decision.clone())}),
            )
        }
        CommandAction::CheckerResult => {
            let result = request
                .payload
                .get("result")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("Checker result must be an object"))?;
            let checker_ref = result
                .get("checkerRef")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("Checker result ref is required"))?;
            let checker_id = required_text(checker_ref, "id", 128)?;
            let version = required_u64(checker_ref, "version", 1, u64::MAX)?;
            if !checker_exists(&projection, checker_id, version) {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "Checker result version is not registered",
                ));
            }
            let ok = result
                .get("ok")
                .and_then(Value::as_bool)
                .ok_or_else(|| invalid_input("Invalid Checker result"))?;
            let timed_out = result
                .get("timedOut")
                .and_then(Value::as_bool)
                .ok_or_else(|| invalid_input("Invalid Checker result"))?;
            let summary = result.get("summary").and_then(Value::as_str).unwrap_or("");
            let output = result.get("output").and_then(Value::as_str).unwrap_or("");
            if summary.chars().count() > 1000 || output.chars().count() > 4000 {
                return Err(invalid_input("Invalid Checker result"));
            }
            let exit_code = match result.get("exitCode") {
                None | Some(Value::Null) => Value::Null,
                Some(Value::Number(value)) if value.as_i64().is_some() => {
                    Value::Number(value.clone())
                }
                Some(_) => return Err(invalid_input("Invalid Checker exitCode")),
            };
            let work_id = result
                .get("workId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(128)
                .collect::<String>();
            let decision_id = request
                .payload
                .get("decision_id")
                .and_then(Value::as_str)
                .unwrap_or("")
                .chars()
                .take(128)
                .collect::<String>();
            (
                "checker_result",
                json!({
                    "result": {
                        "checkerRef": {"id": checker_id, "version": version},
                        "label": required_text(result, "label", 256)?,
                        "ok": ok,
                        "exitCode": exit_code,
                        "timedOut": timed_out,
                        "durationMs": required_u64(result, "durationMs", 0, u64::MAX)?,
                        "reason": required_text(result, "reason", 64)?,
                        "summary": summary,
                        "output": output,
                        "workId": work_id,
                        "timestamp": required_u64(result, "timestamp", 0, u64::MAX)?,
                    },
                    "decisionId": decision_id,
                }),
            )
        }
        CommandAction::WatchAdd | CommandAction::WatchUpdate => {
            let item = request
                .payload
                .get("item")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_input("Watch item must be an object"))?;
            let validated = validated_watch_item(item)?;
            let item_id = validated["id"].as_str().unwrap();
            let existing = watch_index(&projection, item_id);
            if request.action == CommandAction::WatchAdd {
                if let Some(index) = existing {
                    if projection["watch"][index] != validated {
                        return Err(conflict("Project Watch item already exists"));
                    }
                    return command_result(&projection, None);
                }
                if projection["watch"].as_array().unwrap().len() >= MAX_PROJECT_BRAIN_WATCH_ITEMS {
                    return Err(payload_too_large("Project Watch item limit reached"));
                }
                ("watch_added", json!({"item": validated}))
            } else {
                if existing.is_none() {
                    return Err(io::Error::new(
                        io::ErrorKind::NotFound,
                        "Project Watch item not found",
                    ));
                }
                ("watch_updated", json!({"item": validated}))
            }
        }
        CommandAction::WatchDelete => {
            let item_id = required_text(&request.payload, "item_id", 128)?;
            if watch_index(&projection, item_id).is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::NotFound,
                    "Project Watch item not found",
                ));
            }
            ("watch_deleted", json!({"itemId": item_id}))
        }
        CommandAction::CursorConfirm => {
            let conversation_id = required_text(&request.payload, "conversation_id", 256)?;
            let delivered = required_u64(&request.payload, "delivered_sequence", 0, u64::MAX)?;
            let from_sequence = required_u64(&request.payload, "from_sequence", 0, u64::MAX)?;
            let expected = format!(
                "{:x}",
                Sha256::digest(
                    format!(
                        "{}\0{}\0{}\0{}\0{}",
                        transaction.owner_user_id(),
                        request.project_key,
                        conversation_id,
                        from_sequence,
                        delivered
                    )
                    .as_bytes()
                )
            );
            if request
                .payload
                .get("delivery_token")
                .and_then(Value::as_str)
                != Some(&expected)
            {
                return Err(invalid_input("Invalid narrative delivery token"));
            }
            let current = projection["cursors"]
                .get(conversation_id)
                .and_then(|value| value.get("deliveredSequence"))
                .and_then(Value::as_u64)
                .unwrap_or(0);
            if delivered <= current {
                return command_result(&projection, None);
            }
            (
                "cursor_confirmed",
                json!({
                    "conversationId": conversation_id,
                    "deliveredSequence": delivered.min(current_head),
                }),
            )
        }
        CommandAction::CursorPrepare => unreachable!("cursor prepare returns before transition"),
    };
    let sequence = current_head
        .checked_add(1)
        .ok_or_else(|| invalid_data("Project Brain sequence overflow"))?;
    let event = json!({
        "ownerUserId": transaction.owner_user_id(),
        "projectKey": request.project_key,
        "projectSequence": sequence,
        "kind": kind,
        "timestamp": request.timestamp,
        "payload": event_payload,
    });
    fold_event(&mut projection, &event)?;
    append_event(
        database,
        transaction,
        &request.project_key,
        &event,
        sequence,
        request.timestamp,
    )?;
    install_checkpoint_if_due(
        database,
        transaction,
        &request.project_key,
        &mut projection,
        request.timestamp,
    )?;
    continue_checkpoint_retirement(database, transaction, &request.project_key, &projection)?;
    save_projection(
        database,
        transaction,
        &request.project_key,
        &projection,
        request.timestamp,
    )?;
    command_result(&projection, Some(event))
}

fn fold_event(projection: &mut Map<String, Value>, event: &Value) -> io::Result<()> {
    let kind = event.get("kind").and_then(Value::as_str).unwrap_or("");
    let payload = event
        .get("payload")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("Project Brain event payload is malformed"))?;
    let sequence = event
        .get("projectSequence")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Project Brain event sequence is malformed"))?;
    let timestamp = event.get("timestamp").and_then(Value::as_u64).unwrap_or(0);
    match kind {
        "work_started" => {
            let item = payload
                .get("workItem")
                .cloned()
                .ok_or_else(|| invalid_data("Project Brain work event is malformed"))?;
            let work_id = item.get("id").and_then(Value::as_str).unwrap_or("");
            if work_index(projection, work_id).is_none() {
                let items = projection
                    .get_mut("workItems")
                    .and_then(Value::as_array_mut)
                    .ok_or_else(|| invalid_data("Project Brain work items are malformed"))?;
                items.push(item);
                *items = bounded_work_items(items);
            }
        }
        "work_title_refined" => {
            let work_id = payload.get("workId").and_then(Value::as_str).unwrap_or("");
            if let Some(index) = work_index(projection, work_id) {
                let item = projection["workItems"][index].as_object_mut().unwrap();
                let priority = payload
                    .get("titlePriority")
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                if item.get("status").and_then(Value::as_str) == Some("active")
                    && item.get("_titleRefined").and_then(Value::as_bool) != Some(true)
                    && priority
                        > item
                            .get("_titlePriority")
                            .and_then(Value::as_u64)
                            .unwrap_or(0)
                {
                    item.insert("title".to_owned(), payload["title"].clone());
                    item.insert("_titlePriority".to_owned(), Value::from(priority));
                    item.insert("_titleRefined".to_owned(), Value::Bool(true));
                }
            }
        }
        "work_changed" => {
            let work_id = payload.get("workId").and_then(Value::as_str).unwrap_or("");
            if let Some(index) = work_index(projection, work_id) {
                let item = projection["workItems"][index].as_object_mut().unwrap();
                if item.get("status").and_then(Value::as_str) == Some("active") {
                    let mut paths = item["changedPaths"].as_array().unwrap().clone();
                    for path in payload["changedPaths"].as_array().unwrap() {
                        let normalized = path.as_str().unwrap_or("").trim();
                        if !normalized.is_empty()
                            && !paths.iter().any(|path| path.as_str() == Some(normalized))
                        {
                            paths.push(Value::String(normalized.to_owned()));
                        }
                    }
                    if paths.len() > MAX_PROJECT_BRAIN_CHANGED_PATHS {
                        paths.drain(..paths.len() - MAX_PROJECT_BRAIN_CHANGED_PATHS);
                    }
                    item.insert("changedPaths".to_owned(), Value::Array(paths));
                    let mut merged = item["artifacts"].as_array().unwrap().clone();
                    for artifact in payload["artifacts"].as_array().unwrap() {
                        let identity = artifact
                            .get("id")
                            .or_else(|| artifact.get("path"))
                            .and_then(Value::as_str)
                            .unwrap_or("");
                        if identity.is_empty() {
                            continue;
                        }
                        if let Some(existing) = merged.iter().position(|candidate| {
                            candidate
                                .get("id")
                                .or_else(|| candidate.get("path"))
                                .and_then(Value::as_str)
                                == Some(identity)
                        }) {
                            merged.remove(existing);
                        }
                        merged.push(artifact.clone());
                    }
                    if merged.len() > MAX_PROJECT_BRAIN_ARTIFACTS_PER_WORK {
                        merged.drain(..merged.len() - MAX_PROJECT_BRAIN_ARTIFACTS_PER_WORK);
                    }
                    item.insert("artifacts".to_owned(), Value::Array(merged));
                }
            }
        }
        "work_finished" => {
            let work_id = payload.get("workId").and_then(Value::as_str).unwrap_or("");
            if let Some(index) = work_index(projection, work_id) {
                let (has_output, status, summary, title, conversation_id) = {
                    let item = projection["workItems"][index].as_object_mut().unwrap();
                    if item.get("status").and_then(Value::as_str) != Some("active") {
                        projection.insert("headSequence".to_owned(), Value::from(sequence));
                        return Ok(());
                    }
                    item.insert("status".to_owned(), payload["status"].clone());
                    item.insert(
                        "resultSummary".to_owned(),
                        Value::String(
                            payload["resultSummary"]
                                .as_str()
                                .unwrap_or("")
                                .trim()
                                .chars()
                                .take(4000)
                                .collect(),
                        ),
                    );
                    item.insert("finishedAt".to_owned(), Value::from(timestamp));
                    (
                        item["changedPaths"]
                            .as_array()
                            .is_some_and(|value| !value.is_empty())
                            || item["artifacts"]
                                .as_array()
                                .is_some_and(|value| !value.is_empty()),
                        item["status"].as_str().unwrap_or("").to_owned(),
                        item["resultSummary"].as_str().unwrap_or("").to_owned(),
                        item["title"].as_str().unwrap_or("").to_owned(),
                        item["conversationId"].as_str().unwrap_or("").to_owned(),
                    )
                };
                let bounded = bounded_work_items(projection["workItems"].as_array().unwrap());
                projection.insert("workItems".to_owned(), Value::Array(bounded));
                if has_output || matches!(status.as_str(), "failed" | "cancelled") {
                    let narrative = if summary.is_empty() {
                        format!(
                            "{}: {status}",
                            if title.is_empty() { work_id } else { &title }
                        )
                    } else {
                        summary
                    };
                    append_narrative(
                        projection,
                        sequence,
                        "work_result",
                        &narrative,
                        timestamp,
                        work_id,
                        &conversation_id,
                    )?;
                }
            }
        }
        "narrative_added" => append_narrative(
            projection,
            sequence,
            payload
                .get("narrativeKind")
                .and_then(Value::as_str)
                .unwrap_or("note"),
            payload.get("text").and_then(Value::as_str).unwrap_or(""),
            timestamp,
            payload.get("workId").and_then(Value::as_str).unwrap_or(""),
            payload
                .get("conversationId")
                .and_then(Value::as_str)
                .unwrap_or(""),
        )?,
        "checker_registered" => {
            let definition = payload
                .get("definition")
                .cloned()
                .ok_or_else(|| invalid_data("Project Brain checker event is malformed"))?;
            let checker_id = definition
                .get("checkerId")
                .and_then(Value::as_str)
                .unwrap_or("");
            let version = definition
                .get("version")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            if !checker_exists(projection, checker_id, version) {
                projection["checkers"]
                    .as_array_mut()
                    .ok_or_else(|| invalid_data("Project Brain checkers are malformed"))?
                    .push(definition);
            }
        }
        "decision_promoted" => {
            let decision = payload
                .get("decision")
                .cloned()
                .ok_or_else(|| invalid_data("Project Brain decision event is malformed"))?;
            let decision_id = decision
                .get("decisionId")
                .and_then(Value::as_str)
                .unwrap_or("");
            let decisions = projection["charter"]["decisions"]
                .as_array_mut()
                .ok_or_else(|| invalid_data("Project Brain charter is malformed"))?;
            if !decisions
                .iter()
                .any(|item| item.get("decisionId").and_then(Value::as_str) == Some(decision_id))
            {
                decisions.push(decision.clone());
            }
            append_narrative(
                projection,
                sequence,
                "decision",
                decision.get("text").and_then(Value::as_str).unwrap_or(""),
                timestamp,
                "",
                decision
                    .get("sourceConversationId")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )?;
        }
        "checker_result" => {
            let result = payload
                .get("result")
                .cloned()
                .ok_or_else(|| invalid_data("Project Brain checker result is malformed"))?;
            let result_ref = &result["checkerRef"];
            let decision_id = payload
                .get("decisionId")
                .and_then(Value::as_str)
                .unwrap_or("");
            for decision in projection["charter"]["decisions"]
                .as_array_mut()
                .ok_or_else(|| invalid_data("Project Brain charter is malformed"))?
            {
                let decision_ref = &decision["checkerRef"];
                let same_checker = decision_ref.get("id") == result_ref.get("id")
                    && decision_ref.get("version") == result_ref.get("version");
                if same_checker
                    && (decision_id.is_empty()
                        || decision.get("decisionId").and_then(Value::as_str) == Some(decision_id))
                {
                    decision["latestVerification"] = result.clone();
                }
            }
            if result.get("ok").and_then(Value::as_bool) != Some(true) {
                let checker_id = result_ref.get("id").and_then(Value::as_str).unwrap_or("");
                let label = result
                    .get("label")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .or((!checker_id.is_empty()).then_some(checker_id))
                    .unwrap_or("Checker");
                let reason = result
                    .get("summary")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .unwrap_or("checker failed");
                append_narrative(
                    projection,
                    sequence,
                    "checker_failed",
                    &format!("{label}: {reason}"),
                    timestamp,
                    result.get("workId").and_then(Value::as_str).unwrap_or(""),
                    "",
                )?;
            }
        }
        "watch_added" | "watch_updated" | "watch_deleted" => {
            let item = payload.get("item").cloned().unwrap_or_else(|| json!({}));
            let item_id = item
                .get("id")
                .or_else(|| payload.get("itemId"))
                .and_then(Value::as_str)
                .unwrap_or("");
            let watch = projection["watch"]
                .as_array_mut()
                .ok_or_else(|| invalid_data("Project Brain watch is malformed"))?;
            let previous = watch
                .iter()
                .find(|row| row.get("id").and_then(Value::as_str) == Some(item_id))
                .cloned()
                .unwrap_or_else(|| json!({}));
            watch.retain(|row| row.get("id").and_then(Value::as_str) != Some(item_id));
            let (narrative, source_conversation) = if kind == "watch_deleted" {
                (
                    format!(
                        "Watch removed: {}",
                        previous
                            .get("text")
                            .and_then(Value::as_str)
                            .filter(|value| !value.is_empty())
                            .unwrap_or(item_id)
                    ),
                    previous
                        .get("sourceConversationId")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                )
            } else {
                watch.push(item.clone());
                let action = if item.get("status").and_then(Value::as_str) == Some("resolved") {
                    "resolved"
                } else if kind == "watch_added" {
                    "added"
                } else {
                    "updated"
                };
                (
                    format!(
                        "Watch {action}: {}",
                        item.get("text")
                            .and_then(Value::as_str)
                            .filter(|value| !value.is_empty())
                            .unwrap_or(item_id)
                    ),
                    item.get("sourceConversationId")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_owned(),
                )
            };
            append_narrative(
                projection,
                sequence,
                kind,
                &narrative,
                timestamp,
                "",
                &source_conversation,
            )?;
        }
        "cursor_initialized" | "cursor_confirmed" => {
            let conversation_id = payload
                .get("conversationId")
                .and_then(Value::as_str)
                .unwrap_or("");
            if !conversation_id.is_empty() {
                let requested = payload
                    .get("deliveredSequence")
                    .and_then(Value::as_u64)
                    .unwrap_or(sequence);
                let cursors = projection["cursors"]
                    .as_object_mut()
                    .ok_or_else(|| invalid_data("Project Brain cursors are malformed"))?;
                let current = cursors
                    .get(conversation_id)
                    .and_then(|value| value.get("deliveredSequence"))
                    .and_then(Value::as_u64)
                    .unwrap_or(0);
                cursors.insert(
                    conversation_id.to_owned(),
                    json!({"deliveredSequence": current.max(requested), "updatedAt": timestamp}),
                );
                if cursors.len() > MAX_PROJECT_BRAIN_CURSORS {
                    let mut ordered = cursors
                        .iter()
                        .map(|(key, value)| {
                            (
                                value.get("updatedAt").and_then(Value::as_u64).unwrap_or(0),
                                key.clone(),
                            )
                        })
                        .collect::<Vec<_>>();
                    ordered.sort();
                    for (_, key) in &ordered[..ordered.len() - MAX_PROJECT_BRAIN_CURSORS] {
                        cursors.remove(key);
                    }
                }
            }
        }
        _ => return Err(invalid_data("Unsupported Project Brain event kind")),
    }
    projection.insert("headSequence".to_owned(), Value::from(sequence));
    projection.insert("_updatedAt".to_owned(), Value::from(timestamp));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn position(first: u64, last: u64) -> Vec<u8> {
        [first.to_be_bytes(), last.to_be_bytes()].concat()
    }

    #[test]
    fn event_payloads_are_chunked_and_indexed_at_physical_boundaries() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let event = json!({"payload": "x".repeat(EVENT_CHUNK_BYTES + 1)});

        assert_eq!(
            append_event(&database, &mut transaction, "/chunked", &event, 1, 5).unwrap(),
            (1, 2)
        );
        let chunked_index = event_index_key(&transaction, "/chunked", 1).unwrap();
        assert_eq!(
            database
                .entity_get(&mut transaction, &chunked_index)
                .unwrap(),
            Some(position(1, 2))
        );
        database.commit(transaction).unwrap();

        let key = StreamKey::new(7, 11, STREAM_DOMAIN, &project_digest("/chunked")).unwrap();
        let page = database.stream_read(7, 11, &key, 1, 3).unwrap();
        assert_eq!(page.events.len(), 2);
        assert_eq!(&page.events[0].event.payload[..8], EVENT_CHUNK_MAGIC);
        assert_eq!(&page.events[1].event.payload[..8], EVENT_CHUNK_MAGIC);
    }

    #[test]
    fn rebuild_restores_projection_from_the_semantic_event_stream() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let project_key = "/rebuild";
        for (timestamp, text) in [(1, "first"), (2, "second")] {
            let mut transaction = database.begin(7, 11).unwrap();
            command(
                &database,
                &mut transaction,
                &CommandRequest {
                    project_key: project_key.to_owned(),
                    action: CommandAction::NarrativeAdd,
                    payload: json!({"text": text, "kind": "note"})
                        .as_object()
                        .unwrap()
                        .clone(),
                    timestamp,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }

        let mut remove_projection = database.begin(7, 11).unwrap();
        let projection_key = document_key(&remove_projection, project_key).unwrap();
        database
            .entity_delete(&mut remove_projection, projection_key)
            .unwrap();
        database.commit(remove_projection).unwrap();

        let mut transaction = database.begin(7, 11).unwrap();
        let rebuilt = rebuild(&database, &mut transaction, project_key, 3).unwrap();
        let rebuilt: Value = serde_json::from_slice(&rebuilt).unwrap();
        assert_eq!(rebuilt["headSequence"], 2);
        assert_eq!(rebuilt["checkpointSequence"], 0);
        assert_eq!(rebuilt["replayedEvents"], 2);
        assert_eq!(rebuilt["projection"]["narratives"][0]["text"], "first");
        assert_eq!(rebuilt["projection"]["narratives"][1]["text"], "second");
        database.commit(transaction).unwrap();

        let mut inspect = database.begin(7, 11).unwrap();
        let stored: Value =
            serde_json::from_slice(&get(&database, &mut inspect, project_key).unwrap()).unwrap();
        assert_eq!(stored, rebuilt["projection"]);
    }

    #[test]
    fn recovery_snapshot_returns_only_active_work_in_recent_project_order() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (project_key, task_id, timestamp) in
            [("/older", "task-older", 1), ("/newer", "task-newer", 2)]
        {
            let mut transaction = database.begin(7, 11).unwrap();
            let work_id = format!("pw_{:x}", Sha256::digest(task_id.as_bytes()))[..27].to_owned();
            command(
                &database,
                &mut transaction,
                &CommandRequest {
                    project_key: project_key.to_owned(),
                    action: CommandAction::WorkStart,
                    payload: json!({
                        "work_item": {
                            "id": work_id,
                            "taskId": task_id,
                            "conversationId": "conversation",
                            "title": task_id,
                            "trigger": "file_write",
                            "status": "active",
                            "changedPaths": [],
                            "artifacts": [],
                            "resultSummary": "",
                            "startedAt": timestamp,
                            "finishedAt": null,
                            "_titlePriority": 1,
                            "_titleRefined": false
                        }
                    })
                    .as_object()
                    .unwrap()
                    .clone(),
                    timestamp,
                },
            )
            .unwrap();
            database.commit(transaction).unwrap();
        }

        let mut finish = database.begin(7, 11).unwrap();
        let older_work_id = format!("pw_{:x}", Sha256::digest(b"task-older"))[..27].to_owned();
        command(
            &database,
            &mut finish,
            &CommandRequest {
                project_key: "/older".to_owned(),
                action: CommandAction::WorkFinish,
                payload: json!({
                    "work_id": older_work_id,
                    "status": "completed",
                    "result_summary": "done"
                })
                .as_object()
                .unwrap()
                .clone(),
                timestamp: 3,
            },
        )
        .unwrap();
        database.commit(finish).unwrap();

        let mut inspect = database.begin(7, 11).unwrap();
        let snapshot: Value =
            serde_json::from_slice(&recovery_snapshot(&database, &mut inspect).unwrap()).unwrap();
        assert_eq!(snapshot["capped"], false);
        assert_eq!(snapshot["projects"].as_array().unwrap().len(), 1);
        assert_eq!(snapshot["projects"][0]["ownerUserId"], 11);
        assert_eq!(snapshot["projects"][0]["projectKey"], "/newer");
        assert_eq!(
            snapshot["projects"][0]["workItems"][0]["taskId"],
            "task-newer"
        );
        assert!(snapshot["projects"][0]["workItems"][0]
            .get("_titlePriority")
            .is_none());
    }

    #[test]
    fn threshold_commit_installs_checkpoint_and_retires_old_event_indexes() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let project_key = "/checkpoint";
        let mut seed = database.begin(7, 11).unwrap();
        let mut projection = empty_projection(11, project_key);
        projection.insert("headSequence".to_owned(), Value::from(1199_u64));
        save_projection(&database, &mut seed, project_key, &projection, 1).unwrap();
        let project_stream = stream_key(&seed, project_key).unwrap();
        database
            .stream_append(
                &mut seed,
                project_stream,
                1,
                vec![StreamEvent::new(1, "seed", b"seed".to_vec()).unwrap()],
            )
            .unwrap();
        let first_index = event_index_key(&seed, project_key, 1).unwrap();
        database
            .entity_put(&mut seed, first_index, position(1, 1))
            .unwrap();
        let retained_index = event_index_key(&seed, project_key, 602).unwrap();
        database
            .entity_put(&mut seed, retained_index, position(1, 1))
            .unwrap();
        database.commit(seed).unwrap();

        let mut checkpoint = database.begin(7, 11).unwrap();
        let response = command(
            &database,
            &mut checkpoint,
            &CommandRequest {
                project_key: project_key.to_owned(),
                action: CommandAction::NarrativeAdd,
                payload: json!({"text": "checkpoint", "kind": "note"})
                    .as_object()
                    .unwrap()
                    .clone(),
                timestamp: 2,
            },
        )
        .unwrap();
        let response: Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(response["event"]["projectSequence"], 1200);
        assert_eq!(response["projection"]["headSequence"], 1201);
        database.commit(checkpoint).unwrap();

        let mut inspect = database.begin(7, 11).unwrap();
        let first_index = event_index_key(&inspect, project_key, 1).unwrap();
        assert!(database
            .entity_get(&mut inspect, &first_index)
            .unwrap()
            .is_none());
        let retained_index = event_index_key(&inspect, project_key, 602).unwrap();
        assert_eq!(
            database.entity_get(&mut inspect, &retained_index).unwrap(),
            Some(position(1, 1))
        );
        let checkpoint_document = checkpoint_key(&inspect, project_key).unwrap();
        assert!(versioned_document::get_value_with_blob_owner_bounded(
            &database,
            &mut inspect,
            &checkpoint_document,
            "project_brain_checkpoints",
            project_key,
            11,
            MAX_PROJECT_BRAIN_DOCUMENT_BYTES,
        )
        .unwrap()
        .is_some());
        drop(inspect);

        let mut remove_projection = database.begin(7, 11).unwrap();
        let projection_key = document_key(&remove_projection, project_key).unwrap();
        database
            .entity_delete(&mut remove_projection, projection_key)
            .unwrap();
        database.commit(remove_projection).unwrap();

        let mut rebuild_transaction = database.begin(7, 11).unwrap();
        let rebuilt = rebuild(&database, &mut rebuild_transaction, project_key, 3).unwrap();
        let rebuilt: Value = serde_json::from_slice(&rebuilt).unwrap();
        assert_eq!(rebuilt["headSequence"], 1201);
        assert_eq!(rebuilt["checkpointSequence"], 1201);
        assert_eq!(rebuilt["replayedEvents"], 0);
        assert_eq!(rebuilt["projection"]["narratives"][0]["text"], "checkpoint");
        database.commit(rebuild_transaction).unwrap();
    }
}
