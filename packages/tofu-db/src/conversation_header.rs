//! Atomic conversation-header state used by the storage.v1 semantic compiler.
//!
//! This module owns the tenant-global ID claim, owner-local exact count, header
//! document, and reconstructible search-dirty marker. Callers enter only through
//! the bounded Transaction IR steps; physical namespaces are not public API.

use std::io;

use serde_json::{json, Map, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE,
    CONVERSATION_ACTIVITY_CANDIDATE_STATE_NAMESPACE, CONVERSATION_COUNT_NAMESPACE,
    CONVERSATION_DOCUMENT_NAMESPACE, CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
    CONVERSATION_ID_CLAIM_NAMESPACE, CONVERSATION_SEARCH_DIRTY_NAMESPACE,
    CONVERSATION_TRASH_AGE_INDEX_NAMESPACE, CONVERSATION_TRASH_METADATA_NAMESPACE,
    CONVERSATION_TRASH_SETTINGS_OVERLAY_NAMESPACE, CONVERSATION_UPDATED_INDEX_NAMESPACE,
    MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION,
    MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION,
    MAX_PROJECT_RELINK_CONVERSATION_CANDIDATES, MAX_TASK_RESULT_CACHE_FACT,
};

pub(crate) const TENANT_GLOBAL_OWNER_ID: u64 = u64::MAX;
const COUNT_KEY: &[u8] = b"count";
const INDEX_SCAN_PAGE_ROWS: usize = 1_000;
const MAX_INDEX_SCAN_PAGES: usize = 1_024;
const ACTIVITY_CANDIDATE_MAGIC: &[u8; 8] = b"TDBACD01";
const ACTIVITY_CANDIDATE_STATE_MAGIC: &[u8; 8] = b"TDBACS01";
const ACTIVITY_CANDIDATE_STATE_KEY: &[u8] = b"complete";
const ACTIVITY_CANDIDATE_BUILDING_MAGIC: &[u8; 8] = b"TDBASB01";
const ACTIVITY_CANDIDATE_BUILDING_KEY: &[u8] = b"building";

#[derive(Clone, Debug, Eq, PartialEq)]
enum ActivityCandidateIndexState {
    Absent,
    Building(Vec<u8>),
    Complete,
}

pub(crate) struct ActivityCandidateBackfillBatch {
    pub processed_rows: usize,
    pub source_bytes: usize,
    pub complete: bool,
    pub changed: bool,
}

pub(crate) struct CreateRequest {
    pub conversation_id: String,
    pub title: String,
    pub settings_json: Vec<u8>,
    pub created_at_ms: u64,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

pub(crate) struct SettingsUpdateRequest {
    pub conversation_id: String,
    pub updates_json: Vec<u8>,
    pub replace: bool,
    pub expected_settings_json: Option<Vec<u8>>,
    pub expected_revision: Option<u64>,
    pub committed_at_ms: u64,
}

pub(crate) struct MetadataUpdateRequest {
    pub conversation_id: String,
    pub title: Option<String>,
    pub updated_at_ms: Option<u64>,
    pub committed_at_ms: u64,
}

pub(crate) struct DeleteRequest {
    pub conversation_id: String,
    pub deleted_at_ms: u64,
}

pub(crate) struct RestoreRequest {
    pub conversation_id: String,
    pub committed_at_ms: u64,
}

pub(crate) struct CloneRequest {
    pub source_conversation_id: String,
    pub destination_conversation_id: String,
    pub title: Option<String>,
    pub identity_seed: [u8; 32],
    pub committed_at_ms: u64,
}

pub(crate) struct PurgeRequest {
    pub conversation_id: String,
    pub purged_at_ms: u64,
}

pub(crate) struct TrashPruneRequest {
    pub deleted_before_ms: u64,
    pub maximum_conversations: usize,
}

#[derive(Clone, Debug)]
pub struct TurnDefaults {
    pub allow_create: bool,
    pub title: String,
    pub settings_json: Vec<u8>,
    pub created_at_ms: u64,
}

pub(crate) struct CatalogPageRequest {
    pub folder_id: Option<String>,
    pub before_updated_at_ms: Option<u64>,
    pub before_id: String,
    pub limit: usize,
    pub settings_keys: Option<Vec<String>>,
}

#[derive(Clone, Copy)]
pub(crate) enum ListOrder {
    UpdatedDescending,
    IdAscending,
}

pub(crate) struct ListRequest {
    pub project_path: Option<String>,
    pub title_contains: Option<String>,
    pub ids: Option<std::collections::BTreeSet<String>>,
    pub updated_at_gte: Option<i64>,
    pub updated_at_gt: Option<i64>,
    pub created_at_lt: Option<i64>,
    pub order: ListOrder,
    pub settings_keys: Option<Vec<String>>,
    pub include_messages: bool,
    pub limit: usize,
}

pub(crate) struct ActivityDatesRequest {
    pub updated_at_gte: i64,
    pub created_at_lt: Option<i64>,
    pub day_boundaries_ms: Vec<i64>,
    pub limit: usize,
}

pub(crate) struct TaskResultParentSnapshot {
    conversation_id: String,
    header: Map<String, Value>,
    physical_version: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct TaskResultCacheFacts {
    pub cache_prefix_hwm: Option<u64>,
    pub last_turn_cache_read: Option<u64>,
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn conflict() -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, "conversation already exists")
}

fn key(
    tenant_id: u64,
    owner_user_id: u64,
    namespace: &str,
    logical_key: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(tenant_id, owner_user_id, namespace, logical_key)
}

fn decode_count(value: Option<Vec<u8>>) -> io::Result<u64> {
    match value {
        None => Ok(0),
        Some(value) if value.len() == 8 => Ok(u64::from_le_bytes(value.try_into().unwrap())),
        Some(_) => Err(invalid_data("conversation count is malformed")),
    }
}

fn document_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_DOCUMENT_NAMESPACE,
        conversation_id.as_bytes(),
    )
}

pub(crate) fn active_exists(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<bool> {
    Ok(database
        .entity_get(transaction, &document_key(transaction, conversation_id)?)?
        .is_some())
}

fn trash_metadata_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_TRASH_METADATA_NAMESPACE,
        conversation_id.as_bytes(),
    )
}

fn execution_epoch_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
        conversation_id.as_bytes(),
    )
}

pub(crate) fn execution_epoch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<u64> {
    match database.entity_get(
        transaction,
        &execution_epoch_key(transaction, conversation_id)?,
    )? {
        None => Ok(0),
        Some(value) if value.len() == 8 => Ok(u64::from_le_bytes(value.try_into().unwrap())),
        Some(_) => Err(invalid_data("conversation execution epoch is malformed")),
    }
}

pub(crate) fn trash_pin_id(transaction: &AuthorityTransaction, conversation_id: &str) -> Vec<u8> {
    let mut pin_id = Vec::with_capacity(57);
    pin_id.extend_from_slice(b"trash/v1/");
    pin_id.extend_from_slice(&transaction.tenant_id().to_be_bytes());
    pin_id.extend_from_slice(&transaction.owner_user_id().to_be_bytes());
    pin_id.extend_from_slice(blake3::hash(conversation_id.as_bytes()).as_bytes());
    pin_id
}
fn trash_settings_overlay_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_TRASH_SETTINGS_OVERLAY_NAMESPACE,
        conversation_id.as_bytes(),
    )
}

fn read_trash_settings_overlay(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<Map<String, Value>>> {
    let overlay_key = trash_settings_overlay_key(transaction, conversation_id)?;
    let Some(raw) = database.entity_get(transaction, &overlay_key)? else {
        return Ok(None);
    };
    let settings = serde_json::from_slice::<Value>(&raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("conversation trash settings overlay is malformed"))?;
    Ok(Some(settings))
}

fn replace_project_path_references(
    settings: &Map<String, Value>,
    old_path: &str,
    new_path: &str,
) -> Option<Map<String, Value>> {
    let mut rewritten = settings.clone();
    let mut changed = false;
    if rewritten.get("projectPath").and_then(Value::as_str) == Some(old_path) {
        rewritten.insert("projectPath".to_owned(), Value::String(new_path.to_owned()));
        changed = true;
    }
    for plane in ["projectPaths", "readOnlyPaths"] {
        let Some(values) = rewritten.get(plane).and_then(Value::as_array) else {
            continue;
        };
        if !values.iter().any(|item| item.as_str() == Some(old_path)) {
            continue;
        }
        let mut deduplicated: Vec<Value> = Vec::with_capacity(values.len());
        for item in values {
            let candidate = if item.as_str() == Some(old_path) {
                Value::String(new_path.to_owned())
            } else {
                item.clone()
            };
            if !deduplicated.contains(&candidate) {
                deduplicated.push(candidate);
            }
        }
        rewritten.insert(plane.to_owned(), Value::Array(deduplicated));
        changed = true;
    }
    changed.then_some(rewritten)
}

fn trash_age_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    deleted_at_ms: u64,
) -> io::Result<EntityKey> {
    let mut logical_key = Vec::with_capacity(10 + conversation_id.len());
    logical_key.extend_from_slice(&deleted_at_ms.to_be_bytes());
    logical_key.extend_from_slice(
        &u16::try_from(conversation_id.len())
            .map_err(|_| invalid_input("conversation identity exceeds its bound"))?
            .to_be_bytes(),
    );
    logical_key.extend_from_slice(conversation_id.as_bytes());
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_TRASH_AGE_INDEX_NAMESPACE,
        &logical_key,
    )
}

fn decode_trash_age_index_key(key: &EntityKey) -> io::Result<(u64, String)> {
    let encoded = key.key_bytes();
    if encoded.len() < 10 {
        return Err(invalid_data("conversation trash age key is malformed"));
    }
    let deleted_at_ms = u64::from_be_bytes(encoded[..8].try_into().unwrap());
    let identity_bytes = u16::from_be_bytes(encoded[8..10].try_into().unwrap()) as usize;
    if deleted_at_ms == 0 || encoded.len() != 10 + identity_bytes {
        return Err(invalid_data("conversation trash age key is malformed"));
    }
    let conversation_id = std::str::from_utf8(&encoded[10..])
        .map_err(|_| invalid_data("conversation trash identity is not UTF-8"))?
        .to_owned();
    if conversation_id.is_empty() {
        return Err(invalid_data("conversation trash identity is empty"));
    }
    Ok((deleted_at_ms, conversation_id))
}

fn recoverable_ranges(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    updated_at_ms: u64,
) -> io::Result<Vec<(EntityKey, EntityKey)>> {
    let mut ranges = crate::turn::conversation_recoverable_ranges(transaction, conversation_id)?;
    ranges.push(document_key(transaction, conversation_id)?.exact_range()?);
    ranges.push(execution_epoch_key(transaction, conversation_id)?.exact_range()?);
    ranges.push(
        updated_index_key(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            updated_at_ms,
            conversation_id,
        )?
        .exact_range()?,
    );
    ranges.push(
        activity_candidate_key(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            updated_at_ms,
            conversation_id,
        )?
        .exact_range()?,
    );
    ranges.sort_by(|left, right| left.0.cmp(&right.0));
    Ok(ranges)
}

fn updated_index_key(
    tenant_id: u64,
    owner_user_id: u64,
    updated_at_ms: u64,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(9 + conversation_id.len() * 2);
    encoded.extend_from_slice(&(!updated_at_ms).to_be_bytes());
    for byte in conversation_id.as_bytes() {
        encoded.extend_from_slice(&[!byte, 0]);
    }
    // Continuation pairs sort before this terminator, reversing prefix order
    // as well as differing bytes (SQL: updated DESC, id DESC).
    encoded.push(u8::MAX);
    key(
        tenant_id,
        owner_user_id,
        CONVERSATION_UPDATED_INDEX_NAMESPACE,
        &encoded,
    )
}

fn activity_candidate_key(
    tenant_id: u64,
    owner_user_id: u64,
    updated_at_ms: u64,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    let updated = updated_index_key(tenant_id, owner_user_id, updated_at_ms, conversation_id)?;
    EntityKey::new(
        tenant_id,
        owner_user_id,
        CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE,
        updated.key_bytes(),
    )
}

fn activity_candidate_state_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_ACTIVITY_CANDIDATE_STATE_NAMESPACE,
        ACTIVITY_CANDIDATE_STATE_KEY,
    )
}

fn activity_candidate_building_key(transaction: &AuthorityTransaction) -> io::Result<EntityKey> {
    key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_ACTIVITY_CANDIDATE_STATE_NAMESPACE,
        ACTIVITY_CANDIDATE_BUILDING_KEY,
    )
}

fn encode_activity_candidate_cursor(cursor: &[u8]) -> io::Result<Vec<u8>> {
    let cursor_length = u16::try_from(cursor.len())
        .map_err(|_| invalid_data("conversation activity backfill cursor exceeds its bound"))?;
    let mut encoded = Vec::with_capacity(10 + cursor.len());
    encoded.extend_from_slice(ACTIVITY_CANDIDATE_BUILDING_MAGIC);
    encoded.extend_from_slice(&cursor_length.to_be_bytes());
    encoded.extend_from_slice(cursor);
    Ok(encoded)
}

fn decode_activity_candidate_cursor(encoded: &[u8]) -> io::Result<Vec<u8>> {
    if encoded.len() < 10 || &encoded[..8] != ACTIVITY_CANDIDATE_BUILDING_MAGIC {
        return Err(invalid_data(
            "conversation activity backfill state is malformed",
        ));
    }
    let cursor_length = u16::from_be_bytes(encoded[8..10].try_into().unwrap()) as usize;
    if encoded.len() != 10 + cursor_length {
        return Err(invalid_data(
            "conversation activity backfill state is malformed",
        ));
    }
    Ok(encoded[10..].to_vec())
}

fn activity_candidate_index_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<ActivityCandidateIndexState> {
    let complete = database.entity_get(transaction, &activity_candidate_state_key(transaction)?)?;
    let building =
        database.entity_get(transaction, &activity_candidate_building_key(transaction)?)?;
    match (complete, building) {
        (None, None) => Ok(ActivityCandidateIndexState::Absent),
        (Some(value), None) if value == ACTIVITY_CANDIDATE_STATE_MAGIC => {
            Ok(ActivityCandidateIndexState::Complete)
        }
        (None, Some(value)) => {
            decode_activity_candidate_cursor(&value).map(ActivityCandidateIndexState::Building)
        }
        (Some(_), None) => Err(invalid_data(
            "conversation activity candidate state is malformed",
        )),
        (Some(_), Some(_)) => Err(invalid_data(
            "conversation activity candidate states overlap",
        )),
    }
}

fn activity_candidate_index_is_complete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<bool> {
    Ok(matches!(
        activity_candidate_index_state(database, transaction)?,
        ActivityCandidateIndexState::Complete
    ))
}

fn activity_candidate_index_is_maintained(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<bool> {
    Ok(!matches!(
        activity_candidate_index_state(database, transaction)?,
        ActivityCandidateIndexState::Absent
    ))
}

fn owner_has_trashed_conversations(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<bool> {
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_TRASH_METADATA_NAMESPACE,
        b"",
    )?;
    Ok(!database
        .entity_scan(transaction, &start, &end, 1)?
        .is_empty())
}

fn encode_activity_candidate(
    conversation_id: &str,
    created_at_ms: u64,
    updated_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let identity_bytes = u16::try_from(conversation_id.len())
        .map_err(|_| invalid_input("conversation identity exceeds its bound"))?;
    let mut encoded = Vec::with_capacity(26 + conversation_id.len());
    encoded.extend_from_slice(ACTIVITY_CANDIDATE_MAGIC);
    encoded.extend_from_slice(&created_at_ms.to_be_bytes());
    encoded.extend_from_slice(&updated_at_ms.to_be_bytes());
    encoded.extend_from_slice(&identity_bytes.to_be_bytes());
    encoded.extend_from_slice(conversation_id.as_bytes());
    Ok(encoded)
}

fn decode_activity_candidate(encoded: &[u8]) -> io::Result<(String, u64, u64)> {
    if encoded.len() < 26 || &encoded[..8] != ACTIVITY_CANDIDATE_MAGIC {
        return Err(invalid_data("conversation activity candidate is malformed"));
    }
    let created_at_ms = u64::from_be_bytes(encoded[8..16].try_into().unwrap());
    let updated_at_ms = u64::from_be_bytes(encoded[16..24].try_into().unwrap());
    let identity_bytes = u16::from_be_bytes(encoded[24..26].try_into().unwrap()) as usize;
    if encoded.len() != 26 + identity_bytes {
        return Err(invalid_data("conversation activity candidate is malformed"));
    }
    let conversation_id = std::str::from_utf8(&encoded[26..])
        .map_err(|_| invalid_data("conversation activity identity is not UTF-8"))?
        .to_owned();
    if conversation_id.is_empty() {
        return Err(invalid_data("conversation activity identity is empty"));
    }
    Ok((conversation_id, created_at_ms, updated_at_ms))
}

fn put_activity_candidate_if_maintained(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    created_at_ms: u64,
    updated_at_ms: u64,
) -> io::Result<()> {
    if activity_candidate_index_is_maintained(database, transaction)? {
        database.entity_put(
            transaction,
            activity_candidate_key(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                updated_at_ms,
                conversation_id,
            )?,
            encode_activity_candidate(conversation_id, created_at_ms, updated_at_ms)?,
        )?;
    }
    Ok(())
}

fn rekey_activity_candidate_if_maintained(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    created_at_ms: u64,
    previous_updated_at_ms: u64,
    updated_at_ms: u64,
) -> io::Result<()> {
    if activity_candidate_index_is_maintained(database, transaction)? {
        if previous_updated_at_ms != updated_at_ms {
            database.entity_delete(
                transaction,
                activity_candidate_key(
                    transaction.tenant_id(),
                    transaction.owner_user_id(),
                    previous_updated_at_ms,
                    conversation_id,
                )?,
            )?;
        }
        database.entity_put(
            transaction,
            activity_candidate_key(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                updated_at_ms,
                conversation_id,
            )?,
            encode_activity_candidate(conversation_id, created_at_ms, updated_at_ms)?,
        )?;
    }
    Ok(())
}

fn after_index_key(
    key: &EntityKey,
    tenant_id: u64,
    owner_user_id: u64,
    namespace: &str,
) -> io::Result<EntityKey> {
    let mut successor = key.key_bytes().to_vec();
    successor.push(0);
    EntityKey::new(tenant_id, owner_user_id, namespace, &successor)
}

fn header_from_index_value(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
) -> io::Result<(String, serde_json::Map<String, Value>)> {
    let (logical_key, header, _) =
        header_from_index_value_with_bytes(database, tenant_id, owner_user_id, stored)?;
    Ok((logical_key, header))
}

fn header_from_index_value_with_bytes(
    database: &AuthorityDatabase,
    tenant_id: u64,
    owner_user_id: u64,
    stored: &[u8],
) -> io::Result<(String, serde_json::Map<String, Value>, usize)> {
    let (logical_key, value) = crate::versioned_document::materialize_stored_document(
        database,
        tenant_id,
        owner_user_id,
        stored,
        "conversations",
    )?;
    let header = serde_json::from_slice::<Value>(&value)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("conversation covering index value is malformed"))?;
    if header.get("id").and_then(Value::as_str) != Some(&logical_key)
        || header.get("user_id").and_then(Value::as_u64) != Some(owner_user_id)
    {
        return Err(invalid_data(
            "conversation covering index identity is malformed",
        ));
    }
    Ok((logical_key, header, value.len()))
}

fn project_header(
    mut header: serde_json::Map<String, Value>,
    settings_keys: Option<&[String]>,
) -> io::Result<Value> {
    if let Some(settings_keys) = settings_keys {
        let settings = header
            .get("settings")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
        let selected = settings_keys
            .iter()
            .filter_map(|key| settings.get(key).cloned().map(|value| (key.clone(), value)))
            .collect();
        header.insert("settings".to_owned(), Value::Object(selected));
    }
    header.insert("search_text".to_owned(), Value::String(String::new()));
    Ok(json!({"metadata": header, "messages": [], "source": "sidecar"}))
}

fn cursor_allows(
    header: &serde_json::Map<String, Value>,
    before_updated_at_ms: Option<u64>,
    before_id: &str,
) -> io::Result<bool> {
    let Some(before_updated_at_ms) = before_updated_at_ms else {
        return Ok(true);
    };
    let updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let id = header
        .get("id")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("conversation ID is malformed"))?;
    Ok(updated_at_ms < before_updated_at_ms
        || updated_at_ms == before_updated_at_ms && id < before_id)
}

fn load_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<(serde_json::Map<String, Value>, u64)>> {
    let key = document_key(transaction, conversation_id)?;
    let Some(encoded) = crate::versioned_document::get(
        database,
        transaction,
        &key,
        "conversations",
        conversation_id,
    )?
    else {
        return Ok(None);
    };
    let envelope: Value = serde_json::from_slice(&encoded)
        .map_err(|_| invalid_data("conversation document envelope is malformed"))?;
    let physical_version = envelope
        .get("version")
        .and_then(Value::as_u64)
        .filter(|version| *version > 0)
        .ok_or_else(|| invalid_data("conversation document version is malformed"))?;
    let header = envelope
        .get("value")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("conversation header is malformed"))?;
    Ok(Some((header, physical_version)))
}

pub(crate) fn require_active(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    if load_header(database, transaction, conversation_id)?.is_none() {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "conversation not found",
        ));
    }
    Ok(())
}

pub(crate) fn is_active(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<bool> {
    Ok(load_header(database, transaction, conversation_id)?.is_some())
}

pub(crate) fn task_result_parent_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<TaskResultParentSnapshot>> {
    let Some((header, physical_version)) = load_header(database, transaction, conversation_id)?
    else {
        return Ok(None);
    };
    if header.get("id").and_then(Value::as_str) != Some(conversation_id)
        || header.get("user_id").and_then(Value::as_u64) != Some(transaction.owner_user_id())
    {
        return Err(invalid_data("task result parent identity is malformed"));
    }
    if !header.get("settings").is_some_and(Value::is_object) {
        return Err(invalid_data("conversation settings are malformed"));
    }
    Ok(Some(TaskResultParentSnapshot {
        conversation_id: conversation_id.to_owned(),
        header,
        physical_version,
    }))
}

fn task_result_bounded_cache_fact(settings: &Map<String, Value>, field: &str) -> u64 {
    settings
        .get(field)
        .and_then(Value::as_u64)
        .filter(|value| (1..=MAX_TASK_RESULT_CACHE_FACT).contains(value))
        .unwrap_or(0)
}

pub(crate) fn merge_task_result_cache_facts(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    mut parent: TaskResultParentSnapshot,
    requested: TaskResultCacheFacts,
    ambiguous_replay: bool,
    committed_at_ms: u64,
) -> io::Result<TaskResultCacheFacts> {
    let mut settings = parent
        .header
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
    let mut changed = false;
    let cache_prefix_hwm = requested.cache_prefix_hwm.map(|requested| {
        let current = task_result_bounded_cache_fact(&settings, "cachePrefixHWM");
        let merged = current.max(requested);
        if merged != current {
            settings.insert("cachePrefixHWM".to_owned(), Value::from(merged));
            changed = true;
        }
        merged
    });
    let last_turn_cache_read = requested.last_turn_cache_read.map(|requested| {
        let mut current = task_result_bounded_cache_fact(&settings, "lastTurnCacheRead");
        if !ambiguous_replay || current == 0 {
            if current != requested {
                settings.insert("lastTurnCacheRead".to_owned(), Value::from(requested));
                changed = true;
            }
            current = requested;
        }
        current
    });
    if changed {
        let updated_at_ms = parent
            .header
            .get("updated_at")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
        parent
            .header
            .insert("settings".to_owned(), Value::Object(settings));
        store_header(
            database,
            transaction,
            &parent.conversation_id,
            parent.header,
            parent.physical_version,
            committed_at_ms,
        )?;
        let index_value = stored_document_envelope(database, transaction, &parent.conversation_id)?;
        database.entity_put(
            transaction,
            updated_index_key(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                updated_at_ms,
                &parent.conversation_id,
            )?,
            index_value,
        )?;
    }
    Ok(TaskResultCacheFacts {
        cache_prefix_hwm,
        last_turn_cache_read,
    })
}

pub(crate) fn search_projection_updated_at(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<u64>> {
    let Some((header, _)) = load_header(database, transaction, conversation_id)? else {
        return Ok(None);
    };
    if header.get("id").and_then(Value::as_str) != Some(conversation_id)
        || header.get("user_id").and_then(Value::as_u64) != Some(transaction.owner_user_id())
    {
        return Err(invalid_data(
            "conversation search source identity is malformed",
        ));
    }
    header
        .get("updated_at")
        .and_then(Value::as_u64)
        .map(Some)
        .ok_or_else(|| invalid_data("conversation search source timestamp is malformed"))
}

fn store_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    header: serde_json::Map<String, Value>,
    expected_physical_version: u64,
    committed_at_ms: u64,
) -> io::Result<()> {
    let key = document_key(transaction, conversation_id)?;
    let value_json = serde_json::to_vec(&Value::Object(header))
        .map_err(|_| invalid_data("conversation header cannot be encoded"))?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key,
            namespace: "conversations".to_owned(),
            logical_key: conversation_id.to_owned(),
            value_json,
            expected_version: Some(expected_physical_version),
            updated_at_ms: committed_at_ms,
        },
    )?;
    Ok(())
}

fn stored_document_envelope(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    let key = document_key(transaction, conversation_id)?;
    database
        .entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("staged conversation document disappeared"))
}

pub(crate) fn ensure_for_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    defaults: &TurnDefaults,
    now_ms: u64,
    committed_at_ms: u64,
) -> io::Result<()> {
    if load_header(database, transaction, conversation_id)?.is_none() {
        if !defaults.allow_create {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                "conversation not found",
            ));
        }
        create(
            database,
            transaction,
            &CreateRequest {
                conversation_id: conversation_id.to_owned(),
                title: defaults.title.chars().take(500).collect(),
                settings_json: defaults.settings_json.clone(),
                created_at_ms: defaults.created_at_ms,
                updated_at_ms: now_ms,
                committed_at_ms,
            },
        )?;
    }
    let incoming = serde_json::from_slice::<Value>(&defaults.settings_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("turn conversation defaults are malformed"))?;
    if incoming.is_empty() {
        return Ok(());
    }
    let (mut header, physical_version) = load_header(database, transaction, conversation_id)?
        .ok_or_else(|| invalid_data("turn conversation header disappeared"))?;
    let mut settings = header
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
    settings.extend(incoming);
    settings.remove("activeTaskId");
    header.insert("settings".to_owned(), Value::Object(settings));
    store_header(
        database,
        transaction,
        conversation_id,
        header,
        physical_version,
        committed_at_ms,
    )?;
    Ok(())
}

pub(crate) fn advance_for_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    now_ms: u64,
    committed_at_ms: u64,
    increment_message_count: bool,
) -> io::Result<u64> {
    advance_for_turns(
        database,
        transaction,
        conversation_id,
        now_ms,
        committed_at_ms,
        u64::from(increment_message_count),
    )
}

pub(crate) fn advance_for_turns(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    now_ms: u64,
    committed_at_ms: u64,
    added_main_turns: u64,
) -> io::Result<u64> {
    let (mut header, physical_version) = load_header(database, transaction, conversation_id)?
        .ok_or_else(|| invalid_data("turn conversation header disappeared"))?;
    let previous_updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let created_at_ms = header
        .get("created_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
    let revision = header
        .get("rev")
        .and_then(Value::as_u64)
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| invalid_data("conversation revision overflow"))?;
    let message_count = header
        .get("msg_count")
        .and_then(Value::as_u64)
        .and_then(|value| value.checked_add(added_main_turns))
        .ok_or_else(|| invalid_data("conversation message count overflow"))?;
    header.insert("rev".to_owned(), Value::from(revision));
    header.insert("msg_count".to_owned(), Value::from(message_count));
    header.insert("updated_at".to_owned(), Value::from(now_ms));
    store_header(
        database,
        transaction,
        conversation_id,
        header,
        physical_version,
        committed_at_ms,
    )?;
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    database.entity_delete(
        transaction,
        updated_index_key(
            tenant_id,
            owner_user_id,
            previous_updated_at_ms,
            conversation_id,
        )?,
    )?;
    let index_value = stored_document_envelope(database, transaction, conversation_id)?;
    database.entity_put(
        transaction,
        updated_index_key(tenant_id, owner_user_id, now_ms, conversation_id)?,
        index_value,
    )?;
    rekey_activity_candidate_if_maintained(
        database,
        transaction,
        conversation_id,
        created_at_ms,
        previous_updated_at_ms,
        now_ms,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        conversation_id,
    )?;
    Ok(revision)
}

pub(crate) fn advance_for_turn_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    deleted_main_turns: u64,
    now_ms: u64,
    committed_at_ms: u64,
) -> io::Result<u64> {
    let (mut header, physical_version) = load_header(database, transaction, conversation_id)?
        .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "conversation not found"))?;
    let previous_updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let created_at_ms = header
        .get("created_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
    let revision = header
        .get("rev")
        .and_then(Value::as_u64)
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| invalid_data("conversation revision overflow"))?;
    let message_count = header
        .get("msg_count")
        .and_then(Value::as_u64)
        .and_then(|value| value.checked_sub(deleted_main_turns))
        .ok_or_else(|| invalid_data("conversation message count underflow"))?;
    header.insert("rev".to_owned(), Value::from(revision));
    header.insert("msg_count".to_owned(), Value::from(message_count));
    header.insert("updated_at".to_owned(), Value::from(now_ms));
    store_header(
        database,
        transaction,
        conversation_id,
        header,
        physical_version,
        committed_at_ms,
    )?;
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    database.entity_delete(
        transaction,
        updated_index_key(
            tenant_id,
            owner_user_id,
            previous_updated_at_ms,
            conversation_id,
        )?,
    )?;
    let index_value = stored_document_envelope(database, transaction, conversation_id)?;
    database.entity_put(
        transaction,
        updated_index_key(tenant_id, owner_user_id, now_ms, conversation_id)?,
        index_value,
    )?;
    rekey_activity_candidate_if_maintained(
        database,
        transaction,
        conversation_id,
        created_at_ms,
        previous_updated_at_ms,
        now_ms,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        conversation_id,
    )?;
    Ok(revision)
}

pub(crate) fn revision(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<u64> {
    let Some((header, _)) = load_header(database, transaction, conversation_id)? else {
        return Ok(0);
    };
    header
        .get("rev")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation revision is malformed"))
}

pub(crate) fn sync_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<(u64, Map<String, Value>)>> {
    let Some((header, _)) = load_header(database, transaction, conversation_id)? else {
        return Ok(None);
    };
    let revision = header
        .get("rev")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation revision is malformed"))?;
    let settings = header
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
    Ok(Some((revision, settings)))
}

pub(crate) fn create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CreateRequest,
) -> io::Result<()> {
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let claim_key = key(
        tenant_id,
        TENANT_GLOBAL_OWNER_ID,
        CONVERSATION_ID_CLAIM_NAMESPACE,
        request.conversation_id.as_bytes(),
    )?;
    if database.entity_get(transaction, &claim_key)?.is_some() {
        return Err(conflict());
    }

    let count_key = key(
        tenant_id,
        owner_user_id,
        CONVERSATION_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    let current_count = decode_count(database.entity_get(transaction, &count_key)?)?;
    let next_count = current_count
        .checked_add(1)
        .ok_or_else(|| invalid_data("conversation count overflow"))?;
    if current_count == 0
        && matches!(
            activity_candidate_index_state(database, transaction)?,
            ActivityCandidateIndexState::Absent
        )
        && !owner_has_trashed_conversations(database, transaction)?
    {
        database.entity_put(
            transaction,
            activity_candidate_state_key(transaction)?,
            ACTIVITY_CANDIDATE_STATE_MAGIC.to_vec(),
        )?;
    }
    let settings: Value = serde_json::from_slice(&request.settings_json)
        .map_err(|_| invalid_data("conversation settings are malformed"))?;
    if !settings.is_object() {
        return Err(invalid_data("conversation settings are not an object"));
    }
    let document_json = serde_json::to_vec(&json!({
        "id": request.conversation_id,
        "user_id": owner_user_id,
        "title": request.title,
        "created_at": request.created_at_ms,
        "updated_at": request.updated_at_ms,
        "settings": settings,
        "msg_count": 0,
        "rev": 0
    }))
    .map_err(|_| invalid_data("conversation header cannot be encoded"))?;
    let document_key = key(
        tenant_id,
        owner_user_id,
        CONVERSATION_DOCUMENT_NAMESPACE,
        request.conversation_id.as_bytes(),
    )?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: document_key.clone(),
            namespace: "conversations".to_owned(),
            logical_key: request.conversation_id.clone(),
            value_json: document_json,
            expected_version: Some(0),
            updated_at_ms: request.committed_at_ms,
        },
    )?;
    let index_value = database
        .entity_get(transaction, &document_key)?
        .ok_or_else(|| invalid_data("staged conversation document disappeared"))?;
    database.entity_put(transaction, claim_key, owner_user_id.to_be_bytes().to_vec())?;
    database.entity_put(transaction, count_key, next_count.to_le_bytes().to_vec())?;
    database.entity_put(
        transaction,
        updated_index_key(
            tenant_id,
            owner_user_id,
            request.updated_at_ms,
            &request.conversation_id,
        )?,
        index_value,
    )?;
    put_activity_candidate_if_maintained(
        database,
        transaction,
        &request.conversation_id,
        request.created_at_ms,
        request.updated_at_ms,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    database.entity_put(
        transaction,
        execution_epoch_key(transaction, &request.conversation_id)?,
        0_u64.to_le_bytes().to_vec(),
    )?;
    Ok(())
}

pub(crate) fn clone_conversation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CloneRequest,
) -> io::Result<Vec<u8>> {
    let Some((source_header, _)) =
        load_header(database, transaction, &request.source_conversation_id)?
    else {
        return serde_json::to_vec(&json!({
            "cloned": false,
            "missing": true,
            "busy": false
        }))
        .map_err(|_| invalid_data("conversation clone response cannot be encoded"));
    };
    let source_title = source_header
        .get("title")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("source conversation title is malformed"))?;
    let source_title = if source_title.is_empty() {
        "Untitled"
    } else {
        source_title
    };
    let title = request
        .title
        .clone()
        .unwrap_or_else(|| format!("{source_title} (copy)"))
        .chars()
        .take(500)
        .collect::<String>();
    if title.trim().is_empty() {
        return Err(invalid_input("conversation clone title is empty"));
    }
    let mut settings = source_header
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("source conversation settings are malformed"))?;
    settings.remove("activeTaskId");
    settings.remove("_activeAttemptId");
    settings.insert(
        "clonedFrom".to_owned(),
        Value::String(request.source_conversation_id.clone()),
    );
    create(
        database,
        transaction,
        &CreateRequest {
            conversation_id: request.destination_conversation_id.clone(),
            title,
            settings_json: serde_json::to_vec(&Value::Object(settings))
                .map_err(|_| invalid_data("cloned conversation settings cannot be encoded"))?,
            created_at_ms: request.committed_at_ms,
            updated_at_ms: request.committed_at_ms,
            committed_at_ms: request.committed_at_ms,
        },
    )?;
    let summary = crate::turn::clone_conversation_turns(
        database,
        transaction,
        &request.source_conversation_id,
        &request.destination_conversation_id,
        request.identity_seed,
        request.committed_at_ms,
    )?;
    let archive_count = crate::compaction_archive::clone_conversation_archives(
        database,
        transaction,
        &request.source_conversation_id,
        &request.destination_conversation_id,
        request.identity_seed,
        request.committed_at_ms,
    )?;
    let (mut destination_header, physical_version) =
        load_header(database, transaction, &request.destination_conversation_id)?
            .ok_or_else(|| invalid_data("staged cloned conversation disappeared"))?;
    destination_header.insert("msg_count".to_owned(), Value::from(summary.main_count));
    store_header(
        database,
        transaction,
        &request.destination_conversation_id,
        destination_header,
        physical_version,
        request.committed_at_ms,
    )?;
    let index_value =
        stored_document_envelope(database, transaction, &request.destination_conversation_id)?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            request.committed_at_ms,
            &request.destination_conversation_id,
        )?,
        index_value,
    )?;
    serde_json::to_vec(&json!({
        "cloned": true,
        "missing": false,
        "busy": false,
        "conversationId": request.destination_conversation_id,
        "turnCount": summary.turn_count,
        "archiveCount": archive_count,
        "rev": 0
    }))
    .map_err(|_| invalid_data("conversation clone response cannot be encoded"))
}

pub(crate) fn delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &DeleteRequest,
) -> io::Result<Vec<u8>> {
    let metadata_key = trash_metadata_key(transaction, &request.conversation_id)?;
    let Some((header, _)) = load_header(database, transaction, &request.conversation_id)? else {
        let already_deleted = database.entity_get(transaction, &metadata_key)?.is_some();
        return serde_json::to_vec(&json!({
            "deleted": false,
            "alreadyDeleted": already_deleted
        }))
        .map_err(|_| invalid_data("conversation delete response cannot be encoded"));
    };
    let updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let count_key = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    let count = decode_count(database.entity_get(transaction, &count_key)?)?;
    let next_count = count
        .checked_sub(1)
        .ok_or_else(|| invalid_data("conversation count is inconsistent"))?;
    let turn_count =
        crate::turn::conversation_turn_count(database, transaction, &request.conversation_id)?;
    crate::queue::delete_conversation(database, transaction, &request.conversation_id)?;
    crate::timer::delete_conversation(database, transaction, &request.conversation_id)?;
    crate::raw_archive::delete_conversation(database, transaction, &request.conversation_id)?;

    let recoverable_ranges =
        recoverable_ranges(transaction, &request.conversation_id, updated_at_ms)?;
    let pin_id = trash_pin_id(transaction, &request.conversation_id);
    database.stage_persistent_range_snapshot_pin(transaction, &pin_id, &recoverable_ranges)?;
    crate::turn::retire_conversation_attempt_events(
        database,
        transaction,
        &request.conversation_id,
    )?;

    let mut retired_ranges = recoverable_ranges;
    retired_ranges.extend(crate::turn::conversation_executable_ranges(
        transaction,
        &request.conversation_id,
    )?);
    retired_ranges.sort_by(|left, right| left.0.cmp(&right.0));
    for (start, end) in retired_ranges {
        database.entity_retire_range(transaction, &start, &end)?;
    }
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    database.entity_put(transaction, count_key, next_count.to_le_bytes().to_vec())?;
    database.entity_put(
        transaction,
        metadata_key,
        serde_json::to_vec(&json!({
            "conversationId": request.conversation_id,
            "deletedAt": request.deleted_at_ms,
            "turnCount": turn_count
        }))
        .map_err(|_| invalid_data("conversation trash metadata cannot be encoded"))?,
    )?;
    database.entity_put(
        transaction,
        trash_age_index_key(transaction, &request.conversation_id, request.deleted_at_ms)?,
        request.conversation_id.as_bytes().to_vec(),
    )?;
    serde_json::to_vec(&json!({
        "deleted": true,
        "recoverable": true,
        "deletedAt": request.deleted_at_ms
    }))
    .map_err(|_| invalid_data("conversation delete response cannot be encoded"))
}

pub(crate) fn restore(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RestoreRequest,
) -> io::Result<Vec<u8>> {
    if load_header(database, transaction, &request.conversation_id)?.is_some() {
        return serde_json::to_vec(&json!({
            "restored": false,
            "conflict": true,
            "missing": false
        }))
        .map_err(|_| invalid_data("conversation restore response cannot be encoded"));
    }
    let metadata_key = trash_metadata_key(transaction, &request.conversation_id)?;
    let Some(metadata_bytes) = database.entity_get(transaction, &metadata_key)? else {
        return serde_json::to_vec(&json!({
            "restored": false,
            "conflict": false,
            "missing": true
        }))
        .map_err(|_| invalid_data("conversation restore response cannot be encoded"));
    };
    let metadata = serde_json::from_slice::<Value>(&metadata_bytes)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("conversation trash metadata is malformed"))?;
    if metadata.get("conversationId").and_then(Value::as_str)
        != Some(request.conversation_id.as_str())
    {
        return Err(invalid_data("conversation trash identity is malformed"));
    }
    let deleted_at_ms = metadata
        .get("deletedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation trash timestamp is malformed"))?;
    let turn_count = metadata
        .get("turnCount")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation trash Turn count is malformed"))?;
    let pin_id = trash_pin_id(transaction, &request.conversation_id);
    let mut trash = database
        .begin_persistent_snapshot(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &pin_id,
        )?
        .ok_or_else(|| invalid_data("conversation trash capsule is missing"))?;
    let (mut header, _) = load_header(database, &mut trash, &request.conversation_id)?
        .ok_or_else(|| invalid_data("conversation trash header is missing"))?;
    let previous_epoch = execution_epoch(database, &mut trash, &request.conversation_id)?;
    drop(trash);
    if let Some(overlay) =
        read_trash_settings_overlay(database, transaction, &request.conversation_id)?
    {
        header.insert("settings".to_owned(), Value::Object(overlay));
        database.entity_delete(
            transaction,
            trash_settings_overlay_key(transaction, &request.conversation_id)?,
        )?;
    }
    let updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let created_at_ms = header
        .get("created_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
    let revision = header
        .get("rev")
        .and_then(Value::as_u64)
        .and_then(|value| value.checked_add(1))
        .ok_or_else(|| invalid_data("conversation revision overflow"))?;
    let next_epoch = previous_epoch
        .checked_add(1)
        .ok_or_else(|| invalid_data("conversation execution epoch overflow"))?;
    let settings = header
        .get_mut("settings")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
    settings.remove("activeTaskId");
    settings.remove("_activeAttemptId");
    header.insert("rev".to_owned(), Value::from(revision));

    let ranges = recoverable_ranges(transaction, &request.conversation_id, updated_at_ms)?;
    database.stage_persistent_range_snapshot_restore(transaction, &pin_id, &ranges)?;
    store_header(
        database,
        transaction,
        &request.conversation_id,
        header,
        0,
        request.committed_at_ms,
    )?;
    let stored = stored_document_envelope(database, transaction, &request.conversation_id)?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            updated_at_ms,
            &request.conversation_id,
        )?,
        stored,
    )?;
    put_activity_candidate_if_maintained(
        database,
        transaction,
        &request.conversation_id,
        created_at_ms,
        updated_at_ms,
    )?;
    database.entity_put(
        transaction,
        execution_epoch_key(transaction, &request.conversation_id)?,
        next_epoch.to_le_bytes().to_vec(),
    )?;
    let count_key = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    let next_count = decode_count(database.entity_get(transaction, &count_key)?)?
        .checked_add(1)
        .ok_or_else(|| invalid_data("conversation count overflow"))?;
    database.entity_put(transaction, count_key, next_count.to_le_bytes().to_vec())?;
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    database.entity_delete(transaction, metadata_key)?;
    database.entity_delete(
        transaction,
        trash_age_index_key(transaction, &request.conversation_id, deleted_at_ms)?,
    )?;
    if !database.remove_persistent_snapshot_pin(transaction, &pin_id)? {
        return Err(invalid_data("conversation trash capsule disappeared"));
    }
    serde_json::to_vec(&json!({
        "restored": true,
        "conflict": false,
        "missing": false,
        "rev": revision,
        "turnCount": turn_count
    }))
    .map_err(|_| invalid_data("conversation restore response cannot be encoded"))
}

pub(crate) fn purge(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &PurgeRequest,
) -> io::Result<Vec<u8>> {
    let metadata_key = trash_metadata_key(transaction, &request.conversation_id)?;
    let active = load_header(database, transaction, &request.conversation_id)?.is_some();
    let (identities, deleted_at_ms) = if active {
        let identities = crate::turn::conversation_identity_records(
            database,
            transaction,
            &request.conversation_id,
        )?;
        delete(
            database,
            transaction,
            &DeleteRequest {
                conversation_id: request.conversation_id.clone(),
                deleted_at_ms: request.purged_at_ms,
            },
        )?;
        (identities, request.purged_at_ms)
    } else {
        let Some(metadata_bytes) = database.entity_get(transaction, &metadata_key)? else {
            return serde_json::to_vec(&json!({"purged": false}))
                .map_err(|_| invalid_data("conversation purge response cannot be encoded"));
        };
        let metadata = serde_json::from_slice::<Value>(&metadata_bytes)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("conversation trash metadata is malformed"))?;
        if metadata.get("conversationId").and_then(Value::as_str)
            != Some(request.conversation_id.as_str())
        {
            return Err(invalid_data("conversation trash identity is malformed"));
        }
        let deleted_at_ms = metadata
            .get("deletedAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation trash timestamp is malformed"))?;
        let pin_id = trash_pin_id(transaction, &request.conversation_id);
        let mut trash = database
            .begin_persistent_snapshot(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                &pin_id,
            )?
            .ok_or_else(|| invalid_data("conversation trash capsule is missing"))?;
        let identities = crate::turn::conversation_identity_records(
            database,
            &mut trash,
            &request.conversation_id,
        )?;
        drop(trash);
        (identities, deleted_at_ms)
    };

    crate::turn::release_conversation_identity_claims(
        database,
        transaction,
        &request.conversation_id,
        &identities,
    )?;
    crate::compaction_archive::delete_conversation(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let claim_key = key(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        CONVERSATION_ID_CLAIM_NAMESPACE,
        request.conversation_id.as_bytes(),
    )?;
    match database.entity_get(transaction, &claim_key)? {
        Some(owner) if owner == transaction.owner_user_id().to_be_bytes() => {
            database.entity_delete(transaction, claim_key)?;
        }
        Some(_) => return Err(invalid_data("conversation claim owner differs")),
        None => return Err(invalid_data("conversation claim is missing")),
    }
    database.entity_delete(
        transaction,
        trash_settings_overlay_key(transaction, &request.conversation_id)?,
    )?;
    database.entity_delete(transaction, metadata_key)?;
    database.entity_delete(
        transaction,
        trash_age_index_key(transaction, &request.conversation_id, deleted_at_ms)?,
    )?;
    let pin_id = trash_pin_id(transaction, &request.conversation_id);
    if !database.remove_persistent_snapshot_pin(transaction, &pin_id)? {
        return Err(invalid_data("conversation trash capsule disappeared"));
    }
    serde_json::to_vec(&json!({"purged": true}))
        .map_err(|_| invalid_data("conversation purge response cannot be encoded"))
}

pub(crate) fn trash_prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &TrashPruneRequest,
) -> io::Result<Vec<u8>> {
    let start = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_TRASH_AGE_INDEX_NAMESPACE,
        b"",
    )?;
    let end = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_TRASH_AGE_INDEX_NAMESPACE,
        &request.deleted_before_ms.to_be_bytes(),
    )?;
    let mut rows =
        database.entity_scan(transaction, &start, &end, request.maximum_conversations + 1)?;
    let remaining = rows.len() > request.maximum_conversations;
    rows.truncate(request.maximum_conversations);
    let mut purged = 0_usize;
    for (age_key, stored_identity) in rows {
        let (deleted_at_ms, conversation_id) = decode_trash_age_index_key(&age_key)?;
        if deleted_at_ms >= request.deleted_before_ms
            || stored_identity != conversation_id.as_bytes()
        {
            return Err(invalid_data("conversation trash age index is inconsistent"));
        }
        let response = purge(
            database,
            transaction,
            &PurgeRequest {
                conversation_id,
                purged_at_ms: request.deleted_before_ms,
            },
        )?;
        if serde_json::from_slice::<Value>(&response)
            .ok()
            .and_then(|value| value.get("purged").and_then(Value::as_bool))
            != Some(true)
        {
            return Err(invalid_data(
                "conversation trash index points to missing trash",
            ));
        }
        purged += 1;
    }
    serde_json::to_vec(&json!({
        "purgedConversations": purged,
        "remaining": remaining
    }))
    .map_err(|_| invalid_data("conversation trash prune response cannot be encoded"))
}

pub(crate) fn relink_project_settings(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    old_path: &str,
    new_path: &str,
    committed_at_ms: u64,
) -> io::Result<(usize, usize)> {
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let mut active_candidates: Vec<String> = Vec::new();
    let mut trashed_candidates: Vec<String> = Vec::new();
    let mut total = 0_usize;

    let (document_start, document_end) = EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        CONVERSATION_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let mut page_start = document_start;
    loop {
        let rows = database.entity_scan(
            transaction,
            &page_start,
            &document_end,
            INDEX_SCAN_PAGE_ROWS,
        )?;
        if rows.is_empty() {
            break;
        }
        let full_page = rows.len() == INDEX_SCAN_PAGE_ROWS;
        let mut last_key: Option<EntityKey> = None;
        for (row_key, _) in rows {
            let conversation_id = std::str::from_utf8(row_key.key_bytes())
                .map_err(|_| invalid_data("conversation identity is not UTF-8"))?
                .to_owned();
            if let Some((header, _)) = load_header(database, transaction, &conversation_id)? {
                let settings = header
                    .get("settings")
                    .and_then(Value::as_object)
                    .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
                if replace_project_path_references(settings, old_path, new_path).is_some() {
                    total += 1;
                    if total > MAX_PROJECT_RELINK_CONVERSATION_CANDIDATES {
                        return Err(io::Error::new(
                            io::ErrorKind::WouldBlock,
                            "Too many conversations reference the old project path",
                        ));
                    }
                    active_candidates.push(conversation_id);
                }
            }
            last_key = Some(row_key);
        }
        if !full_page {
            break;
        }
        let last_key = last_key.ok_or_else(|| invalid_data("conversation scan page is empty"))?;
        page_start = after_index_key(
            &last_key,
            tenant_id,
            owner_user_id,
            CONVERSATION_DOCUMENT_NAMESPACE,
        )?;
    }

    let (trash_start, trash_end) = EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        CONVERSATION_TRASH_METADATA_NAMESPACE,
        b"",
    )?;
    let mut page_start = trash_start;
    loop {
        let rows = database.entity_scan(
            transaction,
            &page_start,
            &trash_end,
            INDEX_SCAN_PAGE_ROWS,
        )?;
        if rows.is_empty() {
            break;
        }
        let full_page = rows.len() == INDEX_SCAN_PAGE_ROWS;
        let mut last_key: Option<EntityKey> = None;
        for (row_key, _) in rows {
            let conversation_id = std::str::from_utf8(row_key.key_bytes())
                .map_err(|_| invalid_data("conversation trash identity is not UTF-8"))?
                .to_owned();
            let settings = match read_trash_settings_overlay(
                database,
                transaction,
                &conversation_id,
            )? {
                Some(overlay) => overlay,
                None => {
                    let pin_id = trash_pin_id(transaction, &conversation_id);
                    let mut trash = database
                        .begin_persistent_snapshot(tenant_id, owner_user_id, &pin_id)?
                        .ok_or_else(|| invalid_data("conversation trash capsule is missing"))?;
                    let (header, _) = load_header(database, &mut trash, &conversation_id)?
                        .ok_or_else(|| invalid_data("conversation trash header is missing"))?;
                    drop(trash);
                    header
                        .get("settings")
                        .and_then(Value::as_object)
                        .cloned()
                        .ok_or_else(|| invalid_data("conversation settings are malformed"))?
                }
            };
            if replace_project_path_references(&settings, old_path, new_path).is_some() {
                total += 1;
                if total > MAX_PROJECT_RELINK_CONVERSATION_CANDIDATES {
                    return Err(io::Error::new(
                        io::ErrorKind::WouldBlock,
                        "Too many conversations reference the old project path",
                    ));
                }
                trashed_candidates.push(conversation_id);
            }
            last_key = Some(row_key);
        }
        if !full_page {
            break;
        }
        let last_key = last_key.ok_or_else(|| invalid_data("conversation scan page is empty"))?;
        page_start = after_index_key(
            &last_key,
            tenant_id,
            owner_user_id,
            CONVERSATION_TRASH_METADATA_NAMESPACE,
        )?;
    }

    for conversation_id in &active_candidates {
        let (mut header, physical_version) =
            load_header(database, transaction, conversation_id)?
                .ok_or_else(|| invalid_data("conversation candidate disappeared"))?;
        let settings = header
            .get("settings")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
        let rewritten = replace_project_path_references(settings, old_path, new_path)
            .ok_or_else(|| invalid_data("conversation relink candidate changed"))?;
        let updated_at_ms = header
            .get("updated_at")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
        header.insert("settings".to_owned(), Value::Object(rewritten));
        store_header(
            database,
            transaction,
            conversation_id,
            header,
            physical_version,
            committed_at_ms,
        )?;
        let index_value = stored_document_envelope(database, transaction, conversation_id)?;
        database.entity_put(
            transaction,
            updated_index_key(tenant_id, owner_user_id, updated_at_ms, conversation_id)?,
            index_value,
        )?;
    }
    for conversation_id in &trashed_candidates {
        let settings = match read_trash_settings_overlay(database, transaction, conversation_id)? {
            Some(overlay) => overlay,
            None => {
                let pin_id = trash_pin_id(transaction, conversation_id);
                let mut trash = database
                    .begin_persistent_snapshot(tenant_id, owner_user_id, &pin_id)?
                    .ok_or_else(|| invalid_data("conversation trash capsule is missing"))?;
                let (header, _) = load_header(database, &mut trash, conversation_id)?
                    .ok_or_else(|| invalid_data("conversation trash header is missing"))?;
                drop(trash);
                header
                    .get("settings")
                    .and_then(Value::as_object)
                    .cloned()
                    .ok_or_else(|| invalid_data("conversation settings are malformed"))?
            }
        };
        let rewritten = replace_project_path_references(&settings, old_path, new_path)
            .ok_or_else(|| invalid_data("conversation relink candidate changed"))?;
        database.entity_put(
            transaction,
            trash_settings_overlay_key(transaction, conversation_id)?,
            serde_json::to_vec(&Value::Object(rewritten))
                .map_err(|_| invalid_data("conversation trash settings overlay cannot be encoded"))?,
        )?;
    }
    Ok((active_candidates.len(), trashed_candidates.len()))
}
pub(crate) fn count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
) -> io::Result<Vec<u8>> {
    let count_key = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    serde_json::to_vec(
        &json!({"count": decode_count(database.entity_get(transaction, &count_key)?)?}),
    )
    .map_err(|_| invalid_data("conversation count cannot be encoded"))
}

pub(crate) fn backfill_activity_candidates(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    maximum_rows: usize,
) -> io::Result<ActivityCandidateBackfillBatch> {
    if !(1..=MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION).contains(&maximum_rows) {
        return Err(invalid_input(
            "conversation activity backfill row bound is invalid",
        ));
    }
    let state = activity_candidate_index_state(database, transaction)?;
    if state == ActivityCandidateIndexState::Complete {
        return Ok(ActivityCandidateBackfillBatch {
            processed_rows: 0,
            source_bytes: 0,
            complete: true,
            changed: false,
        });
    }
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let cursor = match state {
        ActivityCandidateIndexState::Absent => {
            let (candidate_start, candidate_end) = EntityKey::prefix_range(
                tenant_id,
                owner_user_id,
                CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE,
                b"",
            )?;
            if !database
                .entity_scan(transaction, &candidate_start, &candidate_end, 1)?
                .is_empty()
            {
                return Err(invalid_data(
                    "conversation activity candidate index exists without state",
                ));
            }
            Vec::new()
        }
        ActivityCandidateIndexState::Building(cursor) => cursor,
        ActivityCandidateIndexState::Complete => unreachable!(),
    };
    let (namespace_start, namespace_end) = EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        CONVERSATION_UPDATED_INDEX_NAMESPACE,
        b"",
    )?;
    let start = if cursor.is_empty() {
        namespace_start
    } else {
        let cursor_key = EntityKey::new(
            tenant_id,
            owner_user_id,
            CONVERSATION_UPDATED_INDEX_NAMESPACE,
            &cursor,
        )?;
        after_index_key(
            &cursor_key,
            tenant_id,
            owner_user_id,
            CONVERSATION_UPDATED_INDEX_NAMESPACE,
        )?
    };
    let rows = database.entity_scan(
        transaction,
        &start,
        &namespace_end,
        maximum_rows
            .checked_add(1)
            .ok_or_else(|| invalid_input("conversation activity backfill bound overflow"))?,
    )?;
    let mut processed_rows = 0_usize;
    let mut source_bytes = 0_usize;
    for (source_key, stored) in rows.iter().take(maximum_rows) {
        let (conversation_id, header, header_bytes) =
            header_from_index_value_with_bytes(database, tenant_id, owner_user_id, stored)?;
        let next_source_bytes = source_bytes
            .checked_add(header_bytes)
            .ok_or_else(|| invalid_data("conversation activity backfill byte count overflow"))?;
        if next_source_bytes > MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION {
            if processed_rows == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "conversation activity backfill source row exceeds its byte bound",
                ));
            }
            break;
        }
        let created_at_ms = header
            .get("created_at")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
        let updated_at_ms = header
            .get("updated_at")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
        if updated_index_key(tenant_id, owner_user_id, updated_at_ms, &conversation_id)?
            != *source_key
        {
            return Err(invalid_data(
                "conversation updated index identity is inconsistent",
            ));
        }
        database.maintenance_entity_put(
            transaction,
            activity_candidate_key(tenant_id, owner_user_id, updated_at_ms, &conversation_id)?,
            encode_activity_candidate(&conversation_id, created_at_ms, updated_at_ms)?,
        )?;
        processed_rows += 1;
        source_bytes = next_source_bytes;
    }
    let has_more = rows.len() > processed_rows;
    let building_key = activity_candidate_building_key(transaction)?;
    if has_more {
        let next_cursor = rows[processed_rows - 1].0.key_bytes();
        database.maintenance_entity_put(
            transaction,
            building_key,
            encode_activity_candidate_cursor(next_cursor)?,
        )?;
    } else {
        database.maintenance_entity_delete(transaction, building_key)?;
        database.maintenance_entity_put(
            transaction,
            activity_candidate_state_key(transaction)?,
            ACTIVITY_CANDIDATE_STATE_MAGIC.to_vec(),
        )?;
    }
    Ok(ActivityCandidateBackfillBatch {
        processed_rows,
        source_bytes,
        complete: !has_more,
        changed: true,
    })
}

pub(crate) fn activity_dates(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ActivityDatesRequest,
) -> io::Result<Vec<u8>> {
    if !(2..=367).contains(&request.day_boundaries_ms.len())
        || request
            .day_boundaries_ms
            .windows(2)
            .any(|pair| pair[0] >= pair[1])
        || !(1..=10_000).contains(&request.limit)
    {
        return Err(invalid_input("invalid conversation activity-date request"));
    }
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let compact_candidates = activity_candidate_index_is_complete(database, transaction)?;
    let candidate_namespace = if compact_candidates {
        CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE
    } else {
        CONVERSATION_UPDATED_INDEX_NAMESPACE
    };
    let (mut start, end) =
        EntityKey::prefix_range(tenant_id, owner_user_id, candidate_namespace, b"")?;
    let mut candidates = Vec::with_capacity(request.limit.min(1_000));
    let mut scanned_candidate_rows = 0_u64;
    let mut exhausted_candidate_index = false;
    for page_number in 0..MAX_INDEX_SCAN_PAGES {
        let rows = database.entity_scan(transaction, &start, &end, INDEX_SCAN_PAGE_ROWS)?;
        if rows.is_empty() {
            exhausted_candidate_index = true;
            break;
        }
        let row_count = rows.len();
        let continuation = after_index_key(
            &rows.last().expect("nonempty conversation page").0,
            tenant_id,
            owner_user_id,
            candidate_namespace,
        )?;
        for (candidate_key, stored) in rows {
            scanned_candidate_rows = scanned_candidate_rows
                .checked_add(1)
                .ok_or_else(|| invalid_data("conversation activity candidate count overflow"))?;
            let (conversation_id, created_at_ms, updated_at_ms) = if compact_candidates {
                let decoded = decode_activity_candidate(&stored)?;
                if activity_candidate_key(tenant_id, owner_user_id, decoded.2, &decoded.0)?
                    != candidate_key
                {
                    return Err(invalid_data(
                        "conversation activity candidate identity is inconsistent",
                    ));
                }
                decoded
            } else {
                let (conversation_id, header) =
                    header_from_index_value(database, tenant_id, owner_user_id, &stored)?;
                let updated_at_ms = header
                    .get("updated_at")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
                let created_at_ms = header
                    .get("created_at")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
                (conversation_id, created_at_ms, updated_at_ms)
            };
            if i128::from(updated_at_ms) < i128::from(request.updated_at_gte)
                || request
                    .created_at_lt
                    .is_some_and(|bound| i128::from(created_at_ms) >= i128::from(bound))
            {
                continue;
            }
            candidates.push((
                conversation_id,
                if updated_at_ms == 0 {
                    created_at_ms
                } else {
                    updated_at_ms
                },
            ));
            if candidates.len() == request.limit {
                break;
            }
        }
        if candidates.len() == request.limit {
            break;
        }
        if row_count < INDEX_SCAN_PAGE_ROWS {
            exhausted_candidate_index = true;
            break;
        }
        if page_number + 1 == MAX_INDEX_SCAN_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "conversation activity-date scan exceeds its page budget",
            ));
        }
        start = continuation;
    }
    if compact_candidates && exhausted_candidate_index {
        let count_key = key(
            tenant_id,
            owner_user_id,
            CONVERSATION_COUNT_NAMESPACE,
            COUNT_KEY,
        )?;
        let authoritative_count = decode_count(database.entity_get(transaction, &count_key)?)?;
        if scanned_candidate_rows != authoritative_count {
            return Err(invalid_data(
                "conversation activity candidate count is inconsistent",
            ));
        }
    }
    let mut counts = vec![0_u64; request.day_boundaries_ms.len() - 1];
    let mut remaining_turn_rows = crate::generated_tofudb_ir::MAX_ACTIVITY_TURN_ROWS_PER_QUERY;
    for (conversation_id, fallback_timestamp_ms) in &candidates {
        for interval in crate::turn::activity_intervals(
            database,
            transaction,
            conversation_id,
            *fallback_timestamp_ms,
            &request.day_boundaries_ms,
            &mut remaining_turn_rows,
        )? {
            counts[interval] = counts[interval]
                .checked_add(1)
                .ok_or_else(|| invalid_data("conversation activity-date count overflow"))?;
        }
    }
    serde_json::to_vec(&json!({
        "candidate_count": candidates.len(),
        "counts": counts,
    }))
    .map_err(|_| invalid_data("conversation activity dates cannot be encoded"))
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    include_messages: bool,
    message_window: usize,
    before_sequence: Option<i64>,
) -> io::Result<Option<Vec<u8>>> {
    let Some((mut metadata, _physical_version)) =
        load_header(database, transaction, conversation_id)?
    else {
        return Ok(None);
    };
    for field in ["id", "title"] {
        if metadata.get(field).and_then(Value::as_str).is_none() {
            return Err(invalid_data("conversation header text field is malformed"));
        }
    }
    for field in ["user_id", "created_at", "updated_at", "msg_count", "rev"] {
        if metadata.get(field).and_then(Value::as_u64).is_none() {
            return Err(invalid_data(
                "conversation header integer field is malformed",
            ));
        }
    }
    if metadata
        .get("settings")
        .and_then(Value::as_object)
        .is_none()
        || metadata.get("id").and_then(Value::as_str) != Some(conversation_id)
        || metadata.get("user_id").and_then(Value::as_u64) != Some(transaction.owner_user_id())
    {
        return Err(invalid_data("conversation header identity is malformed"));
    }
    metadata.insert("search_text".to_owned(), Value::String(String::new()));
    let (messages, total_count, start, end) = if include_messages {
        crate::turn::legacy_messages(
            database,
            transaction,
            conversation_id,
            message_window,
            before_sequence,
        )?
    } else {
        (
            Vec::new(),
            metadata
                .get("msg_count")
                .and_then(Value::as_u64)
                .unwrap_or(0) as usize,
            0,
            0,
        )
    };
    if include_messages {
        metadata.insert("msg_count".to_owned(), Value::from(total_count as u64));
    }
    let mut response = json!({
        "metadata": metadata,
        "messages": messages,
        "source": "sidecar"
    });
    if include_messages && message_window != 0 {
        response.as_object_mut().unwrap().insert(
            "message_page".to_owned(),
            json!({"total_count": total_count, "start": start, "end": end}),
        );
    }
    serde_json::to_vec(&response)
        .map(Some)
        .map_err(|_| invalid_data("conversation document cannot be encoded"))
}

pub(crate) fn update_settings(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &SettingsUpdateRequest,
) -> io::Result<Vec<u8>> {
    let Some((mut header, physical_version)) =
        load_header(database, transaction, &request.conversation_id)?
    else {
        return serde_json::to_vec(&json!({"applied": false, "missing": true, "rev": null}))
            .map_err(|_| invalid_data("conversation result cannot be encoded"));
    };
    let revision = header
        .get("rev")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation revision is malformed"))?;
    let updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    if request
        .expected_revision
        .is_some_and(|expected| expected != revision)
    {
        return serde_json::to_vec(&json!({"applied": false, "missing": false, "rev": revision}))
            .map_err(|_| invalid_data("conversation result cannot be encoded"));
    }
    let current = header
        .get("settings")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
    let updates = serde_json::from_slice::<Value>(&request.updates_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("conversation settings update is malformed"))?;
    let merged = if request.replace {
        let expected = request
            .expected_settings_json
            .as_ref()
            .and_then(|value| serde_json::from_slice::<Value>(value).ok())
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("expected conversation settings are malformed"))?;
        if current != expected {
            return serde_json::to_vec(&json!({
                "applied": false,
                "missing": false,
                "conflict": true,
                "rev": revision
            }))
            .map_err(|_| invalid_data("conversation result cannot be encoded"));
        }
        updates
    } else {
        let mut merged = current;
        merged.extend(updates);
        merged
    };
    header.insert("settings".to_owned(), Value::Object(merged));
    store_header(
        database,
        transaction,
        &request.conversation_id,
        header,
        physical_version,
        request.committed_at_ms,
    )?;
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let index_value = stored_document_envelope(database, transaction, &request.conversation_id)?;
    database.entity_put(
        transaction,
        updated_index_key(
            tenant_id,
            owner_user_id,
            updated_at_ms,
            &request.conversation_id,
        )?,
        index_value,
    )?;
    serde_json::to_vec(&json!({
        "applied": true,
        "missing": false,
        "conflict": false,
        "rev": revision
    }))
    .map_err(|_| invalid_data("conversation result cannot be encoded"))
}

pub(crate) fn update_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &MetadataUpdateRequest,
) -> io::Result<Vec<u8>> {
    let Some((mut header, physical_version)) =
        load_header(database, transaction, &request.conversation_id)?
    else {
        return serde_json::to_vec(&json!({"applied": false, "missing": true, "rev": null}))
            .map_err(|_| invalid_data("conversation result cannot be encoded"));
    };
    let revision = header
        .get("rev")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation revision is malformed"))?;
    let previous_updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let created_at_ms = header
        .get("created_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
    if let Some(title) = &request.title {
        header.insert("title".to_owned(), Value::String(title.clone()));
    }
    if let Some(updated_at_ms) = request.updated_at_ms {
        header.insert("updated_at".to_owned(), Value::from(updated_at_ms));
    }
    store_header(
        database,
        transaction,
        &request.conversation_id,
        header,
        physical_version,
        request.committed_at_ms,
    )?;
    let index_value = stored_document_envelope(database, transaction, &request.conversation_id)?;
    if request.updated_at_ms.is_some() {
        let tenant_id = transaction.tenant_id();
        let owner_user_id = transaction.owner_user_id();
        database.entity_delete(
            transaction,
            updated_index_key(
                tenant_id,
                owner_user_id,
                previous_updated_at_ms,
                &request.conversation_id,
            )?,
        )?;
    }
    let effective_updated_at_ms = request.updated_at_ms.unwrap_or(previous_updated_at_ms);
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    database.entity_put(
        transaction,
        updated_index_key(
            tenant_id,
            owner_user_id,
            effective_updated_at_ms,
            &request.conversation_id,
        )?,
        index_value,
    )?;
    rekey_activity_candidate_if_maintained(
        database,
        transaction,
        &request.conversation_id,
        created_at_ms,
        previous_updated_at_ms,
        effective_updated_at_ms,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    serde_json::to_vec(&json!({"applied": true, "missing": false, "rev": revision}))
        .map_err(|_| invalid_data("conversation result cannot be encoded"))
}

pub(crate) fn catalog_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CatalogPageRequest,
) -> io::Result<Vec<u8>> {
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let (namespace_start, namespace_end) = EntityKey::prefix_range(
        tenant_id,
        owner_user_id,
        CONVERSATION_UPDATED_INDEX_NAMESPACE,
        b"",
    )?;
    let mut start = if request.folder_id.is_none() {
        request
            .before_updated_at_ms
            .map(|updated_at_ms| {
                updated_index_key(tenant_id, owner_user_id, updated_at_ms, &request.before_id)
                    .and_then(|key| {
                        after_index_key(
                            &key,
                            tenant_id,
                            owner_user_id,
                            CONVERSATION_UPDATED_INDEX_NAMESPACE,
                        )
                    })
            })
            .transpose()?
            .unwrap_or(namespace_start)
    } else {
        namespace_start
    };
    let mut total_count = if request.folder_id.is_none() {
        let count_key = key(
            tenant_id,
            owner_user_id,
            CONVERSATION_COUNT_NAMESPACE,
            COUNT_KEY,
        )?;
        decode_count(database.entity_get(transaction, &count_key)?)?
    } else {
        0
    };
    let mut items = Vec::with_capacity(request.limit.saturating_add(1));

    'pages: for page_number in 0..MAX_INDEX_SCAN_PAGES {
        let rows =
            database.entity_scan(transaction, &start, &namespace_end, INDEX_SCAN_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_index_key(
            &rows.last().expect("nonempty index page").0,
            tenant_id,
            owner_user_id,
            CONVERSATION_UPDATED_INDEX_NAMESPACE,
        )?;
        for (_, stored) in rows {
            let (_, header) = header_from_index_value(database, tenant_id, owner_user_id, &stored)?;
            let folder_matches = request.folder_id.as_ref().is_none_or(|folder_id| {
                header
                    .get("settings")
                    .and_then(Value::as_object)
                    .and_then(|settings| settings.get("folderId"))
                    .and_then(Value::as_str)
                    == Some(folder_id)
            });
            if !folder_matches {
                continue;
            }
            if request.folder_id.is_some() {
                total_count = total_count
                    .checked_add(1)
                    .ok_or_else(|| invalid_data("conversation catalog count overflow"))?;
            }
            if cursor_allows(&header, request.before_updated_at_ms, &request.before_id)?
                && items.len() <= request.limit
            {
                items.push(project_header(header, request.settings_keys.as_deref())?);
                if request.folder_id.is_none() && items.len() > request.limit {
                    break 'pages;
                }
            }
        }
        if row_count < INDEX_SCAN_PAGE_ROWS {
            break;
        }
        if page_number + 1 == MAX_INDEX_SCAN_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "conversation catalog scan exceeds its bounded page budget",
            ));
        }
        start = continuation;
    }
    let has_more = items.len() > request.limit;
    items.truncate(request.limit);
    let response = serde_json::to_vec(&json!({
        "items": items,
        "total_count": total_count,
        "has_more": has_more
    }))
    .map_err(|_| invalid_data("conversation catalog cannot be encoded"))?;
    if response.len() > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "conversation catalog response exceeds 8 MiB",
        ));
    }
    Ok(response)
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
) -> io::Result<Vec<u8>> {
    if request.limit == 0 {
        return Ok(b"[]".to_vec());
    }
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let namespace = match request.order {
        ListOrder::UpdatedDescending => CONVERSATION_UPDATED_INDEX_NAMESPACE,
        ListOrder::IdAscending => CONVERSATION_DOCUMENT_NAMESPACE,
    };
    let (mut start, end) = EntityKey::prefix_range(tenant_id, owner_user_id, namespace, b"")?;
    let needle = request
        .title_contains
        .as_ref()
        .map(|value| value.to_lowercase());
    let mut documents = Vec::with_capacity(request.limit.min(1_000));
    let mut response_bytes = 2_usize;

    'pages: for page_number in 0..MAX_INDEX_SCAN_PAGES {
        let rows = database.entity_scan(transaction, &start, &end, INDEX_SCAN_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_index_key(
            &rows.last().expect("nonempty conversation page").0,
            tenant_id,
            owner_user_id,
            namespace,
        )?;
        for (_, stored) in rows {
            let (conversation_id, header) =
                header_from_index_value(database, tenant_id, owner_user_id, &stored)?;
            if request
                .ids
                .as_ref()
                .is_some_and(|ids| !ids.contains(&conversation_id))
            {
                continue;
            }
            let settings = header
                .get("settings")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_data("conversation settings are malformed"))?;
            if request.project_path.as_ref().is_some_and(|project_path| {
                settings.get("projectPath").and_then(Value::as_str) != Some(project_path)
            }) {
                continue;
            }
            let title = header
                .get("title")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("conversation title is malformed"))?;
            if needle
                .as_ref()
                .is_some_and(|needle| !title.to_lowercase().contains(needle))
            {
                continue;
            }
            let updated_at_ms = header
                .get("updated_at")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
            let created_at_ms = header
                .get("created_at")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("conversation create timestamp is malformed"))?;
            if request
                .updated_at_gte
                .is_some_and(|bound| i128::from(updated_at_ms) < i128::from(bound))
                || request
                    .updated_at_gt
                    .is_some_and(|bound| i128::from(updated_at_ms) <= i128::from(bound))
                || request
                    .created_at_lt
                    .is_some_and(|bound| i128::from(created_at_ms) >= i128::from(bound))
            {
                continue;
            }
            let mut document = project_header(header, request.settings_keys.as_deref())?;
            if request.include_messages {
                let (messages, total_count, _, _) =
                    crate::turn::legacy_messages(database, transaction, &conversation_id, 0, None)?;
                document["messages"] = Value::Array(messages);
                document["metadata"]["msg_count"] = Value::from(total_count as u64);
            }
            response_bytes = response_bytes
                .checked_add(
                    serde_json::to_vec(&document)
                        .map_err(|_| invalid_data("conversation list cannot be encoded"))?
                        .len()
                        + usize::from(!documents.is_empty()),
                )
                .filter(|bytes| {
                    *bytes <= crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES
                })
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "conversation list response exceeds 8 MiB",
                    )
                })?;
            documents.push(document);
            if documents.len() == request.limit {
                break 'pages;
            }
        }
        if row_count < INDEX_SCAN_PAGE_ROWS {
            break;
        }
        if page_number + 1 == MAX_INDEX_SCAN_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "conversation list scan exceeds its bounded page budget",
            ));
        }
        start = continuation;
    }
    let response = serde_json::to_vec(&documents)
        .map_err(|_| invalid_data("conversation list cannot be encoded"))?;
    if response.len() > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "conversation list response exceeds 8 MiB",
        ));
    }
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;
    use std::sync::Arc;

    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, Operation, Vfs};

    fn create_fixture(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        timestamp: u64,
    ) {
        create(
            database,
            transaction,
            &CreateRequest {
                conversation_id: conversation_id.to_owned(),
                title: "Fixture".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: timestamp,
                updated_at_ms: timestamp,
                committed_at_ms: timestamp,
            },
        )
        .unwrap();
    }

    #[test]
    fn legacy_owner_with_trash_never_claims_a_partial_candidate_index() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&database, &mut first, "legacy-trash", 100);
        database.commit(first).unwrap();

        let mut make_legacy = database.begin(7, 11).unwrap();
        let state = activity_candidate_state_key(&make_legacy).unwrap();
        let candidate = activity_candidate_key(7, 11, 100, "legacy-trash").unwrap();
        database.entity_delete(&mut make_legacy, state).unwrap();
        database.entity_delete(&mut make_legacy, candidate).unwrap();
        database.commit(make_legacy).unwrap();

        let mut delete_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        delete(
            &database,
            &mut delete_transaction,
            &DeleteRequest {
                conversation_id: "legacy-trash".to_owned(),
                deleted_at_ms: 200,
            },
        )
        .unwrap();
        database.commit(delete_transaction).unwrap();

        let mut second = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&database, &mut second, "new-active", 300);
        database.commit(second).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert!(!activity_candidate_index_is_complete(&database, &mut read).unwrap());
        let response = activity_dates(
            &database,
            &mut read,
            &ActivityDatesRequest {
                updated_at_gte: 0,
                created_at_lt: None,
                day_boundaries_ms: vec![0, 1_000],
                limit: 10,
            },
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&response).unwrap(),
            json!({"candidate_count": 1, "counts": [0]})
        );
    }

    #[test]
    fn activity_candidates_are_compact_upgrade_safe_and_count_checked() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut create_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create(
            &database,
            &mut create_transaction,
            &CreateRequest {
                conversation_id: "large-settings".to_owned(),
                title: "Large".to_owned(),
                settings_json: serde_json::to_vec(&json!({
                    "large": "x".repeat(20_000)
                }))
                .unwrap(),
                created_at_ms: 100,
                updated_at_ms: 200,
                committed_at_ms: 200,
            },
        )
        .unwrap();
        database.commit(create_transaction).unwrap();

        let mut inspect = database.begin(7, 11).unwrap();
        assert!(activity_candidate_index_is_complete(&database, &mut inspect).unwrap());
        let (start, end) =
            EntityKey::prefix_range(7, 11, CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE, b"")
                .unwrap();
        let rows = database.entity_scan(&mut inspect, &start, &end, 2).unwrap();
        assert_eq!(rows.len(), 1);
        assert!(rows[0].1.len() < 300);
        assert_eq!(
            decode_activity_candidate(&rows[0].1).unwrap(),
            ("large-settings".to_owned(), 100, 200)
        );
        drop(inspect);

        let request = ActivityDatesRequest {
            updated_at_gte: 0,
            created_at_lt: None,
            day_boundaries_ms: vec![0, 1_000],
            limit: 10,
        };
        let mut indexed = database.begin(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                activity_dates(&database, &mut indexed, &request)
                    .unwrap()
                    .as_slice()
            )
            .unwrap(),
            json!({"candidate_count": 1, "counts": [0]})
        );
        drop(indexed);

        // Simulate an authority created before the candidate index existed.
        let mut remove = database.begin(7, 11).unwrap();
        let candidate = activity_candidate_key(7, 11, 200, "large-settings").unwrap();
        let state = activity_candidate_state_key(&remove).unwrap();
        database.entity_delete(&mut remove, candidate).unwrap();
        database.entity_delete(&mut remove, state).unwrap();
        database.commit(remove).unwrap();
        let mut legacy = database.begin(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                activity_dates(&database, &mut legacy, &request)
                    .unwrap()
                    .as_slice()
            )
            .unwrap(),
            json!({"candidate_count": 1, "counts": [0]})
        );
        drop(legacy);

        // A completeness marker without its promised row is corruption, not
        // an empty result.
        let mut corrupt = database.begin(7, 11).unwrap();
        let state = activity_candidate_state_key(&corrupt).unwrap();
        database
            .entity_put(&mut corrupt, state, ACTIVITY_CANDIDATE_STATE_MAGIC.to_vec())
            .unwrap();
        database.commit(corrupt).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            activity_dates(&database, &mut read, &request)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn activity_candidate_backfill_is_bounded_resumable_and_tracks_foreground_moves() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&database, &mut seed, "a", 100);
        create_fixture(&database, &mut seed, "b", 200);
        create_fixture(&database, &mut seed, "c", 300);
        database.commit(seed).unwrap();

        let mut make_legacy = database.begin(7, 11).unwrap();
        let complete_state_key = activity_candidate_state_key(&make_legacy).unwrap();
        database
            .entity_delete(&mut make_legacy, complete_state_key)
            .unwrap();
        for (conversation_id, timestamp) in [("a", 100), ("b", 200), ("c", 300)] {
            database
                .entity_delete(
                    &mut make_legacy,
                    activity_candidate_key(7, 11, timestamp, conversation_id).unwrap(),
                )
                .unwrap();
        }
        database.commit(make_legacy).unwrap();

        // An uncommitted first batch leaves neither partial candidates nor a
        // building marker behind.
        let mut abandoned = database.begin(7, 11).unwrap();
        let abandoned_batch = backfill_activity_candidates(&database, &mut abandoned, 1).unwrap();
        assert_eq!(abandoned_batch.processed_rows, 1);
        drop(abandoned);
        let mut unchanged = database.begin(7, 11).unwrap();
        assert_eq!(
            activity_candidate_index_state(&database, &mut unchanged).unwrap(),
            ActivityCandidateIndexState::Absent
        );
        let (candidate_start, candidate_end) =
            EntityKey::prefix_range(7, 11, CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE, b"")
                .unwrap();
        assert!(database
            .entity_scan(&mut unchanged, &candidate_start, &candidate_end, 1)
            .unwrap()
            .is_empty());
        drop(unchanged);

        // A foreground mutation prepared against the absent state cannot
        // commit after the building marker is published without maintaining
        // the candidate index.
        let mut stale_foreground = database.begin(7, 11).unwrap();
        update_metadata(
            &database,
            &mut stale_foreground,
            &MetadataUpdateRequest {
                conversation_id: "a".to_owned(),
                title: Some("stale".to_owned()),
                updated_at_ms: None,
                committed_at_ms: 325,
            },
        )
        .unwrap();
        let first = database
            .backfill_conversation_activity_candidates(7, 11, 1)
            .unwrap();
        assert_eq!(first.processed_rows, 1);
        assert!(!first.complete);
        assert!(first.committed);
        assert_eq!(
            database.commit(stale_foreground).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut building_read = database.begin(7, 11).unwrap();
        let building_response = activity_dates(
            &database,
            &mut building_read,
            &ActivityDatesRequest {
                updated_at_gte: 0,
                created_at_lt: None,
                day_boundaries_ms: vec![0, 1_000],
                limit: 10,
            },
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&building_response).unwrap(),
            json!({"candidate_count": 3, "counts": [0]})
        );
        drop(building_read);

        // The already-scanned row crosses behind the cursor, while a new row
        // crosses ahead of it. Building-state foreground maintenance keeps
        // both represented without restarting the scan.
        let mut move_scanned = database.begin(7, 11).unwrap();
        update_metadata(
            &database,
            &mut move_scanned,
            &MetadataUpdateRequest {
                conversation_id: "c".to_owned(),
                title: None,
                updated_at_ms: Some(50),
                committed_at_ms: 350,
            },
        )
        .unwrap();
        database.commit(move_scanned).unwrap();

        let mut create_ahead = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&database, &mut create_ahead, "d", 400);
        database.commit(create_ahead).unwrap();

        let mut delete_unscanned = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        delete(
            &database,
            &mut delete_unscanned,
            &DeleteRequest {
                conversation_id: "b".to_owned(),
                deleted_at_ms: 500,
            },
        )
        .unwrap();
        database.commit(delete_unscanned).unwrap();
        let mut restore_unscanned = database.begin(7, 11).unwrap();
        restore(
            &database,
            &mut restore_unscanned,
            &RestoreRequest {
                conversation_id: "b".to_owned(),
                committed_at_ms: 600,
            },
        )
        .unwrap();
        database.commit(restore_unscanned).unwrap();

        let mut rounds = 1;
        loop {
            let progress = database
                .backfill_conversation_activity_candidates(7, 11, 1)
                .unwrap();
            rounds += 1;
            assert!(rounds < 10);
            if progress.complete {
                break;
            }
        }
        let mut inspect = database.begin(7, 11).unwrap();
        assert!(activity_candidate_index_is_complete(&database, &mut inspect).unwrap());
        let rows = database
            .entity_scan(&mut inspect, &candidate_start, &candidate_end, 10)
            .unwrap();
        assert_eq!(rows.len(), 4);
        let decoded = rows
            .iter()
            .map(|(_, value)| decode_activity_candidate(value).unwrap())
            .collect::<std::collections::BTreeSet<_>>();
        assert_eq!(
            decoded,
            [
                ("a".to_owned(), 100, 100),
                ("b".to_owned(), 200, 200),
                ("c".to_owned(), 300, 50),
                ("d".to_owned(), 400, 400),
            ]
            .into_iter()
            .collect()
        );
        drop(inspect);

        let finished = database
            .backfill_conversation_activity_candidates(7, 11, 1)
            .unwrap();
        assert!(finished.complete);
        assert!(!finished.committed);
        let repeated = database
            .backfill_conversation_activity_candidates(7, 11, 1)
            .unwrap();
        assert_eq!(repeated, finished);
        assert!(database
            .backfill_conversation_activity_candidates(7, 11, 0)
            .is_err());
        assert!(database
            .backfill_conversation_activity_candidates(
                7,
                11,
                MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION + 1,
            )
            .is_err());
    }

    #[test]
    fn activity_candidate_backfill_enforces_the_source_byte_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let large_settings = serde_json::to_vec(&json!({
            "payload": "x".repeat(6 * 1024 * 1024)
        }))
        .unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for (conversation_id, timestamp) in [("large-a", 100), ("large-b", 200), ("large-c", 300)] {
            create(
                &database,
                &mut seed,
                &CreateRequest {
                    conversation_id: conversation_id.to_owned(),
                    title: "Large".to_owned(),
                    settings_json: large_settings.clone(),
                    created_at_ms: timestamp,
                    updated_at_ms: timestamp,
                    committed_at_ms: timestamp,
                },
            )
            .unwrap();
        }
        database.commit(seed).unwrap();
        let mut make_legacy = database.begin(7, 11).unwrap();
        let state_key = activity_candidate_state_key(&make_legacy).unwrap();
        database.entity_delete(&mut make_legacy, state_key).unwrap();
        for (conversation_id, timestamp) in [("large-a", 100), ("large-b", 200), ("large-c", 300)] {
            database
                .entity_delete(
                    &mut make_legacy,
                    activity_candidate_key(7, 11, timestamp, conversation_id).unwrap(),
                )
                .unwrap();
        }
        database.commit(make_legacy).unwrap();

        let first = database
            .backfill_conversation_activity_candidates(7, 11, 3)
            .unwrap();
        assert_eq!(first.processed_rows, 2);
        assert!(first.source_bytes <= MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION);
        assert!(!first.complete);
        let second = database
            .backfill_conversation_activity_candidates(7, 11, 3)
            .unwrap();
        assert_eq!(second.processed_rows, 1);
        assert!(
            second.source_bytes <= MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION
        );
        assert!(second.complete);
    }

    fn prepared_legacy_activity_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let mut create_transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&database, &mut create_transaction, "fault-backfill", 100);
        database.commit(create_transaction).unwrap();
        let mut make_legacy = database.begin(7, 11).unwrap();
        let state_key = activity_candidate_state_key(&make_legacy).unwrap();
        database.entity_delete(&mut make_legacy, state_key).unwrap();
        database
            .entity_delete(
                &mut make_legacy,
                activity_candidate_key(7, 11, 100, "fault-backfill").unwrap(),
            )
            .unwrap();
        database.commit(make_legacy).unwrap();
        drop(database);
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn assert_activity_backfill_recovery_converges(vfs: Arc<DeterministicVfs>, acknowledged: bool) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered =
            AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let mut inspect = recovered.begin(7, 11).unwrap();
        let state = activity_candidate_index_state(&recovered, &mut inspect).unwrap();
        let (start, end) =
            EntityKey::prefix_range(7, 11, CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE, b"")
                .unwrap();
        let rows = recovered
            .entity_scan(&mut inspect, &start, &end, 2)
            .unwrap();
        if acknowledged {
            assert_eq!(state, ActivityCandidateIndexState::Complete);
        }
        match state {
            ActivityCandidateIndexState::Absent => assert!(rows.is_empty()),
            ActivityCandidateIndexState::Complete => {
                assert_eq!(rows.len(), 1);
                assert_eq!(
                    decode_activity_candidate(&rows[0].1).unwrap(),
                    ("fault-backfill".to_owned(), 100, 100)
                );
            }
            ActivityCandidateIndexState::Building(_) => {
                panic!("single-row backfill recovered a partial transaction")
            }
        }
        drop(inspect);
        let progress = recovered
            .backfill_conversation_activity_candidates(7, 11, 1)
            .unwrap();
        assert!(progress.complete);
        let mut final_read = recovered.begin(7, 11).unwrap();
        assert!(activity_candidate_index_is_complete(&recovered, &mut final_read).unwrap());
        assert_eq!(
            recovered
                .entity_scan(&mut final_read, &start, &end, 2)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn every_activity_backfill_commit_fault_recovers_one_complete_prefix() {
        let baseline_vfs = prepared_legacy_activity_vfs();
        let mut baseline =
            AuthorityDatabase::open_with_vfs(Path::new("/data"), baseline_vfs.clone()).unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        baseline
            .backfill_conversation_activity_candidates(7, 11, 1)
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_legacy_activity_vfs();
            let mut database =
                AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let acknowledged = database
                .backfill_conversation_activity_candidates(7, 11, 1)
                .is_ok();
            drop(database);
            assert_activity_backfill_recovery_converges(vfs, acknowledged);
        }
        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_legacy_activity_vfs();
            let mut database =
                AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let acknowledged = database
                .backfill_conversation_activity_candidates(7, 11, 1)
                .is_ok();
            drop(database);
            assert_activity_backfill_recovery_converges(vfs, acknowledged);
        }
        for (index, operation) in trace.iter().enumerate() {
            if !matches!(
                operation,
                Operation::SyncData | Operation::SyncAll | Operation::SyncDirectory
            ) {
                continue;
            }
            let vfs = prepared_legacy_activity_vfs();
            let mut database =
                AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::DropSync,
            }))
            .unwrap();
            let acknowledged = database
                .backfill_conversation_activity_candidates(7, 11, 1)
                .is_ok();
            drop(database);
            assert_activity_backfill_recovery_converges(vfs, acknowledged);
        }
    }
}
