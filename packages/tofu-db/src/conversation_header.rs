//! Atomic conversation-header state used by the storage.v1 semantic compiler.
//!
//! This module owns the tenant-global ID claim, owner-local exact count, header
//! document, and reconstructible search-dirty marker. Callers enter only through
//! the bounded Transaction IR steps; physical namespaces are not public API.

pub(crate) mod migration;
pub(crate) mod recency;
pub(crate) mod trash_migration;

use std::cmp::Reverse;
use std::collections::BinaryHeap;
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

#[derive(Clone)]
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
    ranges.extend(crate::frozen_transcript::recoverable_ranges(
        transaction,
        conversation_id,
    )?);
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
    _updated_at_ms: u64,
    conversation_id: &str,
) -> io::Result<EntityKey> {
    // The timestamp belongs in the value: rekeying on every streamed update
    // leaves an unbounded history of tombstones in the scan path.
    EntityKey::new(
        tenant_id,
        owner_user_id,
        CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE,
        conversation_id.as_bytes(),
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
    database.flush_deferred_indexes(transaction)?;
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
    // Fast path: the steady state is Complete, and the complete marker alone
    // proves maintenance. The building key is only consulted when the
    // complete marker is absent, preserving the full state machine's
    // malformed/overlap detection off the hot path.
    if let Some(value) = database.entity_get(transaction, &activity_candidate_state_key(transaction)?)?
    {
        if value == ACTIVITY_CANDIDATE_STATE_MAGIC {
            return Ok(true);
        }
        return Err(invalid_data(
            "conversation activity candidate state is malformed",
        ));
    }
    match database.entity_get(transaction, &activity_candidate_building_key(transaction)?)? {
        None => Ok(false),
        Some(value) => {
            decode_activity_candidate_cursor(&value)?;
            Ok(true)
        }
    }
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
    _previous_updated_at_ms: u64,
    updated_at_ms: u64,
) -> io::Result<()> {
    if activity_candidate_index_is_maintained(database, transaction)? {
        // The candidate key is timestamp-free by design (the timestamp lives
        // in the value to avoid tombstone history), so the old and new keys
        // are identical: the put alone rekeys, no delete is needed.
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
    // Single-parse hot path: the projection envelope the generic get used to
    // build (parse -> re-serialize -> parse -> clone) collapses into one parse
    // whose object map is moved out untouched.
    let (value, physical_version) =
        match crate::versioned_document::get_value_with_version(
            database,
            transaction,
            &key,
            "conversations",
            conversation_id,
        )? {
            None => return Ok(None),
            Some(parts) => parts,
        };
    let header = match serde_json::from_slice::<Value>(&value) {
        Ok(Value::Object(header)) => header,
        _ => return Err(invalid_data("conversation header is malformed")),
    };
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
        let current = settings
            .get("lastTurnCacheRead")
            .and_then(Value::as_u64)
            .filter(|value| *value <= MAX_TASK_RESULT_CACHE_FACT);
        let merged = if ambiguous_replay {
            current.unwrap_or(requested)
        } else {
            requested
        };
        if Some(merged) != current {
            settings.insert("lastTurnCacheRead".to_owned(), Value::from(merged));
            changed = true;
        }
        merged
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

pub(crate) fn search_projection_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Option<(u64, String)>> {
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
        .map(|time| {
            Some((
                time,
                header
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            ))
        })
        .ok_or_else(|| invalid_data("conversation search source timestamp is malformed"))
}

/// Store the header after an in-transaction `load_header`: the load already
/// registered the document key's point witness on this transaction's
/// snapshot, so the put neither re-reads the key for its CAS check (the
/// caller-supplied version is the same snapshot's value) nor re-parses the
/// machine-produced JSON for validation. OCC conflict detection is identical:
/// a concurrent commit still invalidates the load's witness. `restore` is the
/// one exception caller — see `store_header_with_cas_read`.
fn store_header(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    header: serde_json::Map<String, Value>,
    expected_physical_version: u64,
    committed_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let recency_updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let key = document_key(transaction, conversation_id)?;
    let value_json = serde_json::to_vec(&Value::Object(header))
        .map_err(|_| invalid_data("conversation header cannot be encoded"))?;
    let stored_envelope = crate::versioned_document::put_known_version_returning_envelope(
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
        expected_physical_version,
    )?;
    // Recency-directory maintenance rides the same transaction as the header
    // write (docs/TOFUDB_RECENCY_DIRECTORY.md): upsert with the exact sort
    // key the legacy sidebar comparator reads.
    recency::note_upsert(database, transaction, conversation_id, recency_updated_at_ms)?;
    Ok(stored_envelope)
}

/// Legacy header store for callers that did NOT read the document in this
/// transaction (restore reads it through the trash capsule's snapshot
/// transaction instead). The put performs the generic CAS read so the
/// document key's point witness is registered on this transaction.
fn store_header_with_cas_read(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    header: serde_json::Map<String, Value>,
    expected_physical_version: u64,
    committed_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let recency_updated_at_ms = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
    let key = document_key(transaction, conversation_id)?;
    let value_json = serde_json::to_vec(&Value::Object(header))
        .map_err(|_| invalid_data("conversation header cannot be encoded"))?;
    let (_, stored_envelope) = crate::versioned_document::put_returning_envelope(
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
    recency::note_upsert(database, transaction, conversation_id, recency_updated_at_ms)?;
    Ok(stored_envelope)
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

/// Preserve catalog recency during derived translation writes while advancing revision.
pub(crate) fn activity_timestamp(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<u64> {
    let (header, _) = load_header(database, transaction, conversation_id)?
        .ok_or_else(|| invalid_data("conversation header disappeared"))?;
    header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation activity timestamp is malformed"))
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

/// Fused hot-path equivalent of `ensure_for_turn` + `execution_epoch` +
/// `advance_for_turn` for `turn.append_settled`: the conversation header is
/// loaded and stored once per append when the conversation already exists
/// and carries no settings merge. Creation and settings merges fall back to
/// the legacy three-call sequence so version/CAS outcomes stay identical on
/// those paths. Returns `(execution_epoch, revision)`.
pub(crate) fn advance_for_turn_append(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    defaults: &TurnDefaults,
    now_ms: u64,
    committed_at_ms: u64,
    increment_message_count: bool,
) -> io::Result<(u64, u64)> {
    let Some((mut header, mut physical_version)) =
        load_header(database, transaction, conversation_id)?
    else {
        ensure_for_turn(
            database,
            transaction,
            conversation_id,
            defaults,
            now_ms,
            committed_at_ms,
        )?;
        let epoch = execution_epoch(database, transaction, conversation_id)?;
        let revision = advance_for_turn(
            database,
            transaction,
            conversation_id,
            now_ms,
            committed_at_ms,
            increment_message_count,
        )?;
        return Ok((epoch, revision));
    };
    let epoch = execution_epoch(database, transaction, conversation_id)?;
    let incoming = serde_json::from_slice::<Value>(&defaults.settings_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("turn conversation defaults are malformed"))?;
    if !incoming.is_empty() {
        // Preserve the legacy two-store version sequence for settings merges.
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
            header.clone(),
            physical_version,
            committed_at_ms,
        )?;
        physical_version = physical_version
            .checked_add(1)
            .ok_or_else(|| invalid_data("conversation document version overflow"))?;
    }
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
        .and_then(|value| value.checked_add(u64::from(increment_message_count)))
        .ok_or_else(|| invalid_data("conversation message count overflow"))?;
    header.insert("rev".to_owned(), Value::from(revision));
    header.insert("msg_count".to_owned(), Value::from(message_count));
    header.insert("updated_at".to_owned(), Value::from(now_ms));
    // The stored envelope comes straight from the put instead of a second
    // read of the entity the put just wrote; bytes are identical.
    let index_value = store_header(
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
    Ok((epoch, revision))
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

// Creation count/activity maintenance is sequencer-owned unless a later
// predicate reads it; those reads flush pending effects on the original view.
fn read_count(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction, key: &EntityKey,
) -> io::Result<u64> {
    database.flush_deferred_indexes(transaction)?;
    decode_count(database.entity_get(transaction, key)?)
}

pub(crate) fn apply_created_metadata(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, created_at_ms: u64, updated_at_ms: u64,
) -> io::Result<()> {
    let count_key = key(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_COUNT_NAMESPACE,
        COUNT_KEY,
    )?;
    let current_count = read_count(database, transaction, &count_key)?;
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
    database.entity_put(transaction, count_key, next_count.to_le_bytes().to_vec())?;
    put_activity_candidate_if_maintained(database, transaction, conversation_id,
        created_at_ms, updated_at_ms)
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
    let (_, index_value) = crate::versioned_document::put_returning_envelope(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: document_key,
            namespace: "conversations".to_owned(),
            logical_key: request.conversation_id.clone(),
            value_json: document_json,
            expected_version: Some(0),
            updated_at_ms: request.committed_at_ms,
        },
    )?;
    database.entity_put(transaction, claim_key, owner_user_id.to_be_bytes().to_vec())?;
    if database.is_preparing_transaction() {
        database.defer_index_update(transaction, recency::DeferredMutation::Created {
            conversation_id: request.conversation_id.clone(),
            created_at_ms: request.created_at_ms,
            updated_at_ms: request.updated_at_ms,
        })?;
    } else {
        apply_created_metadata(database, transaction, &request.conversation_id,
            request.created_at_ms, request.updated_at_ms)?;
    }
    recency::note_upsert(
        database,
        transaction,
        &request.conversation_id,
        request.updated_at_ms,
    )?;
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
    let frozen_count = crate::frozen_transcript::clone_history(
        database,
        transaction,
        &request.source_conversation_id,
        &request.destination_conversation_id,
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
    destination_header.insert(
        "msg_count".to_owned(),
        Value::from(if summary.main_count == 0 {
            frozen_count as u64
        } else {
            summary.main_count
        }),
    );
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
    database: &mut AuthorityDatabase,
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
    let count = read_count(database, transaction, &count_key)?;
    let next_count = count
        .checked_sub(1)
        .ok_or_else(|| invalid_data("conversation count is inconsistent"))?;
    let turn_count =
        crate::turn::conversation_turn_count(database, transaction, &request.conversation_id)?;
    crate::queue::delete_conversation(database, transaction, &request.conversation_id)?;
    crate::timer::delete_conversation(database, transaction, &request.conversation_id)?;
    crate::raw_archive::delete_conversation(database, transaction, &request.conversation_id)?;
    // Derived model continuations are discarded, never archived with trash.
    crate::model_continuation::delete_conversation(
        database,
        transaction,
        &request.conversation_id,
    )?;

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
    recency::note_remove(database, transaction, &request.conversation_id)?;
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
    database: &mut AuthorityDatabase,
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
    // Restore advances the execution epoch, which changes public_turn
    // output for older turns: archived at-append messages would diverge
    // from the exact read, so the archive falls back until rebuilt.
    crate::transcript_archive::invalidate(
        database,
        transaction,
        &request.conversation_id,
    )?;
    store_header_with_cas_read(
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
    let next_count = read_count(database, transaction, &count_key)?
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
    database: &mut AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &PurgeRequest,
) -> io::Result<Vec<u8>> {
    crate::transcript_archive::invalidate(
        database,
        transaction,
        &request.conversation_id,
    )?;
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
    crate::compaction_archive::delete_conversation_all_scopes(
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
    database: &mut AuthorityDatabase,
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
        let rows =
            database.entity_scan(transaction, &page_start, &trash_end, INDEX_SCAN_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let full_page = rows.len() == INDEX_SCAN_PAGE_ROWS;
        let mut last_key: Option<EntityKey> = None;
        for (row_key, _) in rows {
            let conversation_id = std::str::from_utf8(row_key.key_bytes())
                .map_err(|_| invalid_data("conversation trash identity is not UTF-8"))?
                .to_owned();
            let settings =
                match read_trash_settings_overlay(database, transaction, &conversation_id)? {
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
            serde_json::to_vec(&Value::Object(rewritten)).map_err(|_| {
                invalid_data("conversation trash settings overlay cannot be encoded")
            })?,
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
        &json!({"count": read_count(database, transaction, &count_key)?}),
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
        CONVERSATION_DOCUMENT_NAMESPACE,
        b"",
    )?;
    let start = if cursor.is_empty() {
        namespace_start
    } else {
        let cursor_key = EntityKey::new(
            tenant_id,
            owner_user_id,
            CONVERSATION_DOCUMENT_NAMESPACE,
            &cursor,
        )?;
        after_index_key(
            &cursor_key,
            tenant_id,
            owner_user_id,
            CONVERSATION_DOCUMENT_NAMESPACE,
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
        if document_key(transaction, &conversation_id)? != *source_key {
            return Err(invalid_data(
                "conversation activity backfill document identity is inconsistent",
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
        CONVERSATION_DOCUMENT_NAMESPACE
    };
    let (mut start, end) =
        EntityKey::prefix_range(tenant_id, owner_user_id, candidate_namespace, b"")?;
    let mut candidates = BinaryHeap::with_capacity(request.limit.min(1_000));
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
            // Keep only the newest requested candidates, with SQL's id DESC
            // tie-break, while scanning stable IDs in bounded pages.
            let candidate = Reverse((updated_at_ms, conversation_id, created_at_ms));
            if candidates.len() < request.limit {
                candidates.push(candidate);
            } else if candidates.peek().is_some_and(|oldest| candidate.0 > oldest.0) {
                candidates.pop();
                candidates.push(candidate);
            }
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
        let authoritative_count = read_count(database, transaction, &count_key)?;
        if scanned_candidate_rows != authoritative_count {
            return Err(invalid_data(
                "conversation activity candidate count is inconsistent",
            ));
        }
    }
    let mut counts = vec![0_u64; request.day_boundaries_ms.len() - 1];
    let mut remaining_turn_rows = crate::generated_tofudb_ir::MAX_ACTIVITY_TURN_ROWS_PER_QUERY;
    for Reverse((updated_at_ms, conversation_id, created_at_ms)) in &candidates {
        let fallback_timestamp_ms = if *updated_at_ms == 0 {
            *created_at_ms
        } else {
            *updated_at_ms
        };
        for interval in crate::turn::activity_intervals(
            database,
            transaction,
            conversation_id,
            fallback_timestamp_ms,
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
    deadline_elapsed: &mut dyn FnMut() -> bool,
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
            deadline_elapsed,
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

// Small catalogs use the primary headers. The updated-time index retains OCC
// tombstones from every historical update; its physical size need not track
// current owner cardinality. Keep both row and byte budgets on this plan.
fn primary_catalog_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CatalogPageRequest,
    owner_count: u64,
) -> io::Result<Vec<u8>> {
    let mut settings_keys = request.settings_keys.clone();
    if request.folder_id.is_some() {
        if let Some(keys) = &mut settings_keys {
            if !keys.iter().any(|key| key == "folderId") {
                keys.push("folderId".to_owned());
            }
        }
    }
    let encoded = list(
        database,
        transaction,
        &ListRequest {
            project_path: None, title_contains: None, ids: None,
            updated_at_gte: None, updated_at_gt: None, created_at_lt: None,
            order: ListOrder::IdAscending, settings_keys, include_messages: false,
            limit: owner_count as usize + 1,
        },
        // Headers-only catalog: no payload materialization, so the deadline
        // check inside `list` can never fire here.
        &mut || false,
    )?;
    let documents: Vec<Value> = serde_json::from_slice(&encoded)
        .map_err(|_| invalid_data("conversation primary catalog is malformed"))?;
    if documents.len() as u64 != owner_count {
        return Err(invalid_data("conversation primary catalog count mismatch"));
    }
    let mut headers = Vec::with_capacity(documents.len());
    let mut total_count = 0_u64;
    for mut document in documents {
        let header = document.get_mut("metadata").and_then(Value::as_object_mut)
            .ok_or_else(|| invalid_data("conversation catalog metadata is malformed"))?;
        if request.folder_id.as_ref().is_some_and(|folder| {
            header.get("settings").and_then(|settings| settings.get("folderId"))
                .and_then(Value::as_str) != Some(folder.as_str())
        }) { continue; }
        total_count += 1;
        if !cursor_allows(header, request.before_updated_at_ms, &request.before_id)? { continue; }
        let updated = header.get("updated_at").and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
        let id = header.get("id").and_then(Value::as_str)
            .ok_or_else(|| invalid_data("conversation ID is malformed"))?.to_owned();
        headers.push((updated, id, std::mem::take(header)));
    }
    headers.sort_unstable_by(|a, b| b.0.cmp(&a.0).then_with(|| b.1.cmp(&a.1)));
    let has_more = headers.len() > request.limit;
    let items = headers.into_iter().take(request.limit)
        .map(|(_, _, header)| project_header(header, request.settings_keys.as_deref()))
        .collect::<io::Result<Vec<_>>>()?;
    let response = serde_json::to_vec(&json!({
        "items": items, "total_count": total_count, "has_more": has_more,
    })).map_err(|_| invalid_data("conversation catalog cannot be encoded"))?;
    if response.len() > crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory,
            "conversation catalog response exceeds 8 MiB"));
    }
    Ok(response)
}

pub(crate) fn catalog_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CatalogPageRequest,
) -> io::Result<Vec<u8>> {
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    let count_key = key(tenant_id, owner_user_id, CONVERSATION_COUNT_NAMESPACE, COUNT_KEY)?;
    let owner_count = read_count(database, transaction, &count_key)?;
    if owner_count <= INDEX_SCAN_PAGE_ROWS as u64 {
        return primary_catalog_page(database, transaction, request, owner_count);
    }
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
        read_count(database, transaction, &count_key)?
    } else {
        0
    };
    let mut items = Vec::with_capacity(request.limit.saturating_add(1));

    'pages: for page_number in 0..MAX_INDEX_SCAN_PAGES {
        // The unfiltered cursor already seeks to the first eligible index
        // key. One extra result is sufficient to establish has_more. Folder
        // queries still scan their full bounded pages to compute total_count.
        let page_rows = if request.folder_id.is_none() {
            (request.limit.saturating_add(1) - items.len()).min(INDEX_SCAN_PAGE_ROWS)
        } else {
            INDEX_SCAN_PAGE_ROWS
        };
        let rows = database.entity_scan(transaction, &start, &namespace_end, page_rows)?;
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
        if row_count < page_rows {
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

// Updated-time indexes retain historical OCC tombstones. For small owners,
// read bounded primary headers, apply filters, sort, then hydrate only selected
// transcripts. Query cost follows live owner cardinality, not update history.
fn primary_ordered_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
    owner_count: u64,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<u8>> {
    let mut primary = request.clone();
    primary.order = ListOrder::IdAscending;
    primary.settings_keys = None;
    primary.include_messages = false;
    primary.limit = owner_count as usize + 1;
    let encoded = list(database, transaction, &primary, deadline_elapsed)?;
    let documents: Vec<Value> = serde_json::from_slice(&encoded)
        .map_err(|_| invalid_data("conversation primary list is malformed"))?;
    let mut headers = Vec::with_capacity(documents.len());
    for document in documents {
        let header = document.get("metadata").and_then(Value::as_object)
            .ok_or_else(|| invalid_data("conversation list metadata is malformed"))?.clone();
        let updated = header.get("updated_at").and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("conversation update timestamp is malformed"))?;
        let id = header.get("id").and_then(Value::as_str)
            .ok_or_else(|| invalid_data("conversation ID is malformed"))?.to_owned();
        headers.push((updated, id, header));
    }
    headers.sort_unstable_by(|a, b| b.0.cmp(&a.0).then_with(|| b.1.cmp(&a.1)));
    let maximum_bytes = if request.include_messages {
        crate::generated_tofudb_ir::MAX_FROZEN_TRANSCRIPT_PAGE_BYTES
    } else { crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES };
    let mut response = vec![b'['];
    for (_, id, header) in headers.into_iter().take(request.limit) {
        let mut document = project_header(header, request.settings_keys.as_deref())?;
        if request.include_messages {
            if deadline_elapsed() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "conversation list message hydration deadline elapsed",
                ));
            }
            let (messages, count, _, _) = crate::turn::legacy_messages(
                database,
                transaction,
                &id,
                0,
                None,
                deadline_elapsed,
            )?;
            document["messages"] = Value::Array(messages);
            document["metadata"]["msg_count"] = Value::from(count as u64);
        }
        let bytes = serde_json::to_vec(&document)
            .map_err(|_| invalid_data("conversation list cannot be encoded"))?;
        let separator = usize::from(response.len() > 1);
        if response.len().checked_add(separator + bytes.len() + 1)
            .is_none_or(|length| length > maximum_bytes) {
            return Err(io::Error::new(io::ErrorKind::OutOfMemory,
                "conversation list response exceeds its byte budget"));
        }
        if separator != 0 { response.push(b','); }
        response.extend_from_slice(&bytes);
    }
    response.push(b']');
    Ok(response)
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ListRequest,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<u8>> {
    let maximum_response_bytes = if request.include_messages {
        crate::generated_tofudb_ir::MAX_FROZEN_TRANSCRIPT_PAGE_BYTES
    } else {
        crate::generated_tofudb_ir::MAX_TRANSACTION_IR_LITERAL_BYTES
    };

    if request.limit == 0 {
        return Ok(b"[]".to_vec());
    }
    let tenant_id = transaction.tenant_id();
    let owner_user_id = transaction.owner_user_id();
    if matches!(request.order, ListOrder::UpdatedDescending) {
        // Covered requests are served from the per-owner recency directory
        // (docs/TOFUDB_RECENCY_DIRECTORY.md); every gate failure returns the
        // exact legacy answer through the paths below.
        if let Some(encoded) =
            recency::try_directory_list(database, transaction, request, deadline_elapsed)?
        {
            return Ok(encoded);
        }
        let count_key = key(tenant_id, owner_user_id, CONVERSATION_COUNT_NAMESPACE, COUNT_KEY)?;
        let owner_count = read_count(database, transaction, &count_key)?;
        if owner_count <= INDEX_SCAN_PAGE_ROWS as u64 {
            return primary_ordered_list(database, transaction, request, owner_count, deadline_elapsed);
        }
    }
    let namespace = match request.order {
        ListOrder::UpdatedDescending => CONVERSATION_UPDATED_INDEX_NAMESPACE,
        ListOrder::IdAscending => CONVERSATION_DOCUMENT_NAMESPACE,
    };
    let (mut start, end) = EntityKey::prefix_range(tenant_id, owner_user_id, namespace, b"")?;
    let needle = request
        .title_contains
        .as_ref()
        .map(|value| value.to_lowercase());
    let mut documents: Vec<Vec<u8>> = Vec::with_capacity(request.limit.min(1_000));
    let mut response_bytes = 2_usize;
    // Projection and ordering do not discard rows. Every row filter must be
    // absent before limiting scan work to the requested result count; filtered
    // queries keep their existing page size and total scan allowance.
    let unfiltered = request.project_path.is_none()
        && request.title_contains.is_none()
        && request.ids.is_none()
        && request.updated_at_gte.is_none()
        && request.updated_at_gt.is_none()
        && request.created_at_lt.is_none();

    'pages: for page_number in 0..MAX_INDEX_SCAN_PAGES {
        let page_rows = if unfiltered {
            (request.limit - documents.len()).min(INDEX_SCAN_PAGE_ROWS)
        } else {
            INDEX_SCAN_PAGE_ROWS
        };
        let rows = database.entity_scan(transaction, &start, &end, page_rows)?;
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
                // Message hydration materializes the conversation's full
                // main lane; there is no per-conversation resumption cursor
                // inside a list response, so exhaustion fails the operation
                // instead of silently degrading one entry's history.
                if deadline_elapsed() {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "conversation list message hydration deadline elapsed",
                    ));
                }
                let (messages, total_count, _, _) = crate::turn::legacy_messages(
                    database,
                    transaction,
                    &conversation_id,
                    0,
                    None,
                    deadline_elapsed,
                )?;
                document["messages"] = Value::Array(messages);
                document["metadata"]["msg_count"] = Value::from(total_count as u64);
            }
            let document_bytes = serde_json::to_vec(&document)
                .map_err(|_| invalid_data("conversation list cannot be encoded"))?;
            response_bytes = response_bytes
                .checked_add(document_bytes.len() + usize::from(!documents.is_empty()))
                .filter(|bytes| *bytes <= maximum_response_bytes)
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "conversation list response exceeds its byte budget",
                    )
                })?;
            documents.push(document_bytes);
            if documents.len() == request.limit {
                break 'pages;
            }
        }
        if row_count < page_rows {
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
    // Serialize each row once; joining with commas is byte-identical to
    // serializing the array, and response_bytes tracked the same total.
    let mut response = Vec::with_capacity(response_bytes);
    response.push(b'[');
    for (index, document_bytes) in documents.iter().enumerate() {
        if index > 0 {
            response.push(b',');
        }
        response.extend_from_slice(document_bytes);
    }
    response.push(b']');
    if response.len() > maximum_response_bytes {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "conversation list response exceeds its byte budget",
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

    fn deadline_fixture_conversation(database: &mut AuthorityDatabase, id: &str, with_turn: bool) {
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create(
            database,
            &mut tx,
            &CreateRequest {
                conversation_id: id.to_owned(),
                title: format!("Deadline fixture {id}"),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        if with_turn {
            crate::turn::append_settled(
                database,
                &mut tx,
                &crate::turn::AppendSettledRequest {
                    conversation_id: id.to_owned(),
                    actor: "assistant".to_owned(),
                    status: "pending".to_owned(),
                    projection_json:
                        br#"{"content":"","thinking":"","segments":[],"toolRounds":[]}"#.to_vec(),
                    projection_timestamp: None,
                    settlement_json: b"{}".to_vec(),
                    lane_id: "main".to_owned(),
                    command_id: format!("command-{id}"),
                    kind: "reply".to_owned(),
                    run_id: String::new(),
                    turn_id: format!("{id}-turn"),
                    attempt_id: Some(format!("{id}-attempt")),
                    created_at_ms: 1,
                    committed_at_ms: 1,
                    defaults: TurnDefaults {
                        allow_create: false,
                        title: String::new(),
                        settings_json: b"{}".to_vec(),
                        created_at_ms: 1,
                    },
                },
            )
            .unwrap();
        }
        database.commit(tx).unwrap();
    }

    #[test]
    fn list_include_messages_fails_loud_when_the_deadline_elapses() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        deadline_fixture_conversation(&mut database, "conv-a", false);
        deadline_fixture_conversation(&mut database, "conv-b", false);
        for order in [ListOrder::IdAscending, ListOrder::UpdatedDescending] {
            let request = ListRequest {
                project_path: None,
                title_contains: None,
                ids: None,
                updated_at_gte: None,
                updated_at_gt: None,
                created_at_lt: None,
                order,
                settings_keys: None,
                include_messages: true,
                limit: 10,
            };
            let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            // Unlimited budget: both conversations hydrate (empty lanes).
            let listed = list(&database, &mut tx, &request, &mut || false).unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&listed)
                    .unwrap()
                    .as_array()
                    .unwrap()
                    .len(),
                2
            );
            // The first hydration check passes, the second reports the
            // deadline spent: a list cannot degrade one entry's history
            // silently, so the whole operation fails as `deadline_elapsed`.
            let mut checks = 0;
            let error = list(&database, &mut tx, &request, &mut || {
                checks += 1;
                checks > 1
            })
            .unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        }
    }

    #[test]
    fn get_with_messages_fails_loud_when_the_deadline_elapses() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        deadline_fixture_conversation(&mut database, "conv-get", true);
        let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        // Baseline: the message window reads through under an open budget.
        let full = get(&database, &mut tx, "conv-get", true, 0, None, &mut || false)
            .unwrap()
            .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&full).unwrap()["messages"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        // A spent budget surfaces as `deadline_elapsed` instead of a window
        // the caller cannot distinguish from a complete history.
        let error = get(&database, &mut tx, "conv-get", true, 0, None, &mut || true)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }

    #[test]
    fn small_owner_ordered_lists_preserve_filters_limits_and_settings() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs).unwrap();
        for (id, updated, project) in [("a", 10, "/one"), ("b", 20, "/two"), ("c", 20, "/one")] {
            let mut tx = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            create(&database, &mut tx, &CreateRequest {
                conversation_id: id.to_owned(), title: format!("Fixture {id}"),
                settings_json: serde_json::to_vec(&json!({"projectPath":project,"retained":true})).unwrap(),
                created_at_ms: 1, updated_at_ms: updated, committed_at_ms: updated,
            }).unwrap();
            database.commit(tx).unwrap();
        }
        let mut request = ListRequest {
            project_path: None, title_contains: None, ids: None,
            updated_at_gte: None, updated_at_gt: None, created_at_lt: None,
            order: ListOrder::UpdatedDescending,
            settings_keys: Some(vec!["retained".to_owned()]), include_messages: false, limit: 2,
        };
        let read = |request: &ListRequest| {
            let mut tx = database.begin(7, 11).unwrap();
            serde_json::from_slice::<Value>(
                &list(&database, &mut tx, request, &mut || false).unwrap(),
            )
            .unwrap()
        };
        let rows = read(&request);
        assert_eq!(rows[0]["metadata"]["id"], "c");
        assert_eq!(rows[1]["metadata"]["id"], "b");
        assert_eq!(rows[0]["metadata"]["settings"], json!({"retained":true}));
        request.project_path = Some("/one".to_owned());
        request.updated_at_gte = Some(10);
        request.created_at_lt = Some(2);
        request.include_messages = true;
        let rows = read(&request);
        assert_eq!(rows[0]["metadata"]["id"], "c");
        assert_eq!(rows[1]["metadata"]["id"], "a");
        assert_eq!(rows[0]["messages"], json!([]));
        request.ids = Some(["a".to_owned()].into_iter().collect());
        request.title_contains = Some("TURE A".to_owned());
        assert_eq!(read(&request).as_array().unwrap().len(), 1);
        request.updated_at_gt = Some(10);
        assert_eq!(read(&request), json!([]));
        request.updated_at_gt = None;
        request.limit = 0;
        assert_eq!(read(&request), json!([]));
    }

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

    fn legacy_append_sequence(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
        defaults: &TurnDefaults,
        now_ms: u64,
        increment: bool,
    ) -> (u64, u64) {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        ensure_for_turn(
            database,
            &mut transaction,
            conversation_id,
            defaults,
            now_ms,
            now_ms,
        )
        .unwrap();
        let epoch = execution_epoch(database, &mut transaction, conversation_id).unwrap();
        let revision = advance_for_turn(
            database,
            &mut transaction,
            conversation_id,
            now_ms,
            now_ms,
            increment,
        )
        .unwrap();
        database.commit(transaction).unwrap();
        (epoch, revision)
    }

    fn fused_append_sequence(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
        defaults: &TurnDefaults,
        now_ms: u64,
        increment: bool,
    ) -> (u64, u64) {
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let outcome = advance_for_turn_append(
            database,
            &mut transaction,
            conversation_id,
            defaults,
            now_ms,
            now_ms,
            increment,
        )
        .unwrap();
        database.commit(transaction).unwrap();
        outcome
    }

    fn header_state(
        database: &mut AuthorityDatabase,
        conversation_id: &str,
    ) -> (serde_json::Map<String, Value>, u64) {
        let mut read = database.begin(7, 11).unwrap();
        load_header(database, &mut read, conversation_id)
            .unwrap()
            .expect("fixture conversation exists")
    }

    #[test]
    fn advance_for_turn_append_matches_the_legacy_three_call_sequence() {
        let defaults = TurnDefaults {
            allow_create: false,
            title: "Fixture".to_owned(),
            settings_json: b"{}".to_vec(),
            created_at_ms: 100,
        };
        // Keep directories alive until their databases have dropped. Dropping
        // inline TempDir temporaries removes live authority paths before commit.
        let legacy_directory = tempfile::tempdir().unwrap();
        let fused_directory = tempfile::tempdir().unwrap();
        let mut legacy = AuthorityDatabase::initialize(legacy_directory.path()).unwrap();
        let mut fused = AuthorityDatabase::initialize(fused_directory.path()).unwrap();
        let mut seed = legacy.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&legacy, &mut seed, "conversation", 100);
        legacy.commit(seed).unwrap();
        let mut seed = fused.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&fused, &mut seed, "conversation", 100);
        fused.commit(seed).unwrap();

        // Empty-settings appends take the fused single-store path.
        for (now, increment) in [(200u64, true), (300, true), (400, false)] {
            let expected =
                legacy_append_sequence(&mut legacy, "conversation", &defaults, now, increment);
            let actual =
                fused_append_sequence(&mut fused, "conversation", &defaults, now, increment);
            assert_eq!(expected, actual);
        }
        assert_eq!(
            header_state(&mut legacy, "conversation"),
            header_state(&mut fused, "conversation"),
            "fused header state diverged from the legacy sequence"
        );

        // Settings merges fall back to the legacy two-store version sequence.
        let merge_defaults = TurnDefaults {
            allow_create: false,
            title: "Fixture".to_owned(),
            settings_json: br#"{"activeTaskId":"task-9","projectPath":"/perf"}"#.to_vec(),
            created_at_ms: 100,
        };
        let expected =
            legacy_append_sequence(&mut legacy, "conversation", &merge_defaults, 500, true);
        let actual = fused_append_sequence(&mut fused, "conversation", &merge_defaults, 500, true);
        assert_eq!(expected, actual);
        let (legacy_header, fused_header) = (
            header_state(&mut legacy, "conversation"),
            header_state(&mut fused, "conversation"),
        );
        assert_eq!(legacy_header, fused_header);
        let settings = fused_header
            .0
            .get("settings")
            .and_then(Value::as_object)
            .unwrap();
        assert_eq!(
            settings.get("projectPath").and_then(Value::as_str),
            Some("/perf")
        );
        assert!(!settings.contains_key("activeTaskId"));

        // Missing conversation without allow_create is a NotFound in both paths.
        let missing_defaults = TurnDefaults {
            allow_create: false,
            ..defaults.clone()
        };
        let mut fused_missing = fused.begin_with_identity_claim_scopes(7, 11).unwrap();
        let error = advance_for_turn_append(
            &fused,
            &mut fused_missing,
            "missing",
            &missing_defaults,
            600,
            600,
            true,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::NotFound);

        // Missing conversation with allow_create creates exactly like the
        // legacy sequence (create revision 1, then advance to 2).
        let create_defaults = TurnDefaults {
            allow_create: true,
            title: "Created".to_owned(),
            settings_json: b"{}".to_vec(),
            created_at_ms: 100,
        };
        let expected = legacy_append_sequence(
            &mut legacy,
            "created-conversation",
            &create_defaults,
            700,
            true,
        );
        let actual = fused_append_sequence(
            &mut fused,
            "created-conversation",
            &create_defaults,
            700,
            true,
        );
        assert_eq!(expected, actual);
        assert_eq!(
            header_state(&mut legacy, "created-conversation"),
            header_state(&mut fused, "created-conversation"),
        );
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
            &mut database,
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
    fn activity_candidates_survive_stream_churn_and_keep_newest_limit() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        create_fixture(&database, &mut seed, "a", 100);
        create_fixture(&database, &mut seed, "z", 200);
        database.commit(seed).unwrap();
        // More updates than the engine's scan-leaf budget, as a real streamed
        // conversation produces. The activity key must stay physically stable.
        for timestamp in 1_000..2_100 {
            let mut write = database.begin(7, 11).unwrap();
            update_metadata(&database, &mut write, &MetadataUpdateRequest {
                conversation_id: "a".to_owned(), title: None,
                updated_at_ms: Some(timestamp), committed_at_ms: timestamp,
            }).unwrap();
            database.commit(write).unwrap();
        }
        drop(database);
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(activity_candidate_key(7, 11, 100, "a").unwrap(),
                   activity_candidate_key(7, 11, 2_099, "a").unwrap());
        let response = activity_dates(&database, &mut read, &ActivityDatesRequest {
            updated_at_gte: 0, created_at_lt: None,
            day_boundaries_ms: vec![0, 3_000], limit: 1,
        }).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&response).unwrap()["candidate_count"], 1);
        // Selecting newest is independent of stable-key scan order, and old
        // creation-time filters still apply before LIMIT.
        let response = activity_dates(&database, &mut read, &ActivityDatesRequest {
            updated_at_gte: 2_000, created_at_lt: Some(150),
            day_boundaries_ms: vec![0, 3_000], limit: 1,
        }).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&response).unwrap()["candidate_count"], 1);
        let (start, end) = EntityKey::prefix_range(7, 11,
            CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE, b"").unwrap();
        let rows = database.entity_scan(&mut read, &start, &end, 10).unwrap();
        assert_eq!(rows.len(), 2);
        assert!(rows.iter().all(|(_, bytes)| bytes.len() < 64));
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
            &mut database,
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
            &mut database,
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

pub(crate) fn set_frozen_message_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    count: u64,
) -> io::Result<()> {
    require_active(database, transaction, conversation_id)?;
    if crate::turn::main_turn_count(database, transaction, conversation_id)? != 0 {
        return Ok(());
    }
    set_imported_message_count(database, transaction, conversation_id, count)
}

/// Reconstructs the derived sidebar count without inventing a source revision
/// or moving the source header clock during offline Turn/page import.
pub(crate) fn set_imported_message_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    count: u64,
) -> io::Result<()> {
    let (mut header, version) = load_header(database, transaction, conversation_id)?
        .ok_or_else(|| invalid_data("imported transcript parent missing"))?;
    let at = header
        .get("updated_at")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("frozen transcript parent timestamp missing"))?;
    header.insert("msg_count".to_owned(), Value::from(count));
    let envelope = store_header(database, transaction, conversation_id, header, version, at)?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            at,
            conversation_id,
        )?,
        envelope,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        CONVERSATION_SEARCH_DIRTY_NAMESPACE,
        conversation_id,
    )?;
    Ok(())
}
