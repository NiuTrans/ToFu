//! Canonical owner-scoped Turn authority and ordered transcript indexes.
//!
//! Turn documents may overflow to blobs, while small entity records keep lane
//! allocation, ordered reads, search invalidation, and sync replay atomic with
//! the conversation header and command receipt.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::io;

use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use uuid::Uuid;

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::conversation_header::{TurnDefaults, TENANT_GLOBAL_OWNER_ID};
use crate::entity::EntityKey;
use crate::generated_tofudb_ir::{
    ATTEMPT_CLAIM_LOCATOR_VERSION, ATTEMPT_COMMAND_NAMESPACE, ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
    ATTEMPT_EVENT_HEAD_NAMESPACE, ATTEMPT_EVENT_NAMESPACE, ATTEMPT_EVENT_RETENTION_INDEX_NAMESPACE,
    ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE, ATTEMPT_ID_CLAIM_NAMESPACE,
    ATTEMPT_RECOVERY_INDEX_NAMESPACE, ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE,
    ATTEMPT_TIMING_TASK_INDEX_NAMESPACE, ATTEMPT_TURN_COUNT_NAMESPACE,
    ATTEMPT_TURN_DIRECTORY_NAMESPACE, CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
    CONVERSATION_SYNC_AGE_INDEX_NAMESPACE, CONVERSATION_SYNC_EVENT_NAMESPACE,
    CONVERSATION_SYNC_HEAD_NAMESPACE, GENERATION_ATTEMPT_NAMESPACE, MAX_ATTEMPTS_PER_TURN,
    MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION, MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES,
    MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION, MAX_ATTEMPT_TOMBSTONE_BYTES,
    MAX_ATTEMPT_TURN_DIRECTORY_BYTES, MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY,
    MAX_RECOVERY_INDEX_ROWS_PER_OWNER, MAX_SYNC_PRUNE_MATERIALIZED_BYTES,
    MAX_SYNC_PRUNE_ROWS_PER_TRANSACTION, MAX_TIMING_TRACE_CLIENT_OBSERVATIONS,
    MAX_TIMING_TRACE_COUNTER, MAX_TIMING_TRACE_PERSISTED_BYTES, MAX_TIMING_TRACE_ROWS_PER_QUERY,
    MAX_TIMING_TRACE_TASK_CANDIDATES, MAX_TRANSACTION_IR_LITERAL_BYTES,
    MAX_TURN_COMPACTION_MATERIALIZED_BYTES, MAX_TURN_COMPACTION_METADATA_ROWS,
    MAX_TURN_COMPACTION_PROJECTION_BYTES, MAX_TURN_COMPACTION_PROJECTION_UPDATES,
    MAX_TURN_COMPACTION_REPARENTED_TURNS, MAX_TURN_PROJECTION_HEAD_PATCHES,
    MAX_TURN_PROJECTION_INLINE_LIVE_BYTES, MAX_TURN_PROJECTION_PATCH_BYTES,
    MAX_WORKER_JOB_PAYLOAD_BYTES, TURN_ACTIVITY_INDEX_NAMESPACE, TURN_DOCUMENT_NAMESPACE,
    TURN_ID_CLAIM_NAMESPACE, TURN_LANE_COMPACTION_INDEX_NAMESPACE, TURN_LANE_COUNT_NAMESPACE,
    TURN_LANE_HEAD_NAMESPACE, TURN_LANE_INDEX_NAMESPACE, TURN_LANE_LIVE_ATTEMPT_NAMESPACE,
    TURN_PROJECTION_HEAD_NAMESPACE, TURN_SEARCH_DIRTY_NAMESPACE,
    TURN_TOMBSTONE_AGE_INDEX_NAMESPACE, TURN_TOMBSTONE_NAMESPACE, TURN_UPDATED_INDEX_NAMESPACE,
};

const DOCUMENT_IDENTITY: &str = "turns";
const SYNC_DOCUMENT_IDENTITY: &str = "conversation_sync_events";
const ATTEMPT_EVENT_DOCUMENT_IDENTITY: &str = "attempt_events";
const PROJECTION_HEAD_DOCUMENT_IDENTITY: &str = "turn_projection_heads";
const INDEX_PAGE_ROWS: usize = 1_000;
const MAX_LIST_ROWS: usize = 10_000;
const MAX_SCAN_PAGES: usize = 11;
const MAX_DELTA_ROWS: usize = 2_000;
const MAX_DELETE_ROWS: usize = 2_000;
const MAX_CLONE_ROWS: usize = 2_000;
pub(crate) const MAX_RELATED_TURNS_PER_ANNOUNCEMENT: usize = 2_000;
pub(crate) const MAX_VISIBLE_TURNS_PER_SYNCHRONIZATION: usize = 2_000;
const MAX_DELETE_BRANCH_LANES: usize = 256;
const MAX_TOMBSTONE_PRUNE_ROWS: usize = 256;
const SEARCH_PROJECTION_PAGE_ROWS: usize = 8;
const SEARCH_PROJECTION_PAGE_BYTES: u64 = 2_000_000;
const SEARCH_TEXT_MAX_BYTES: usize = 10_000;
const TOMBSTONE_RETENTION_MS: u64 = 7 * 24 * 60 * 60 * 1_000;
const DELTA_OVERLAP_MS: u64 = 5_000;
const UPDATED_INDEX_MAGIC: &[u8; 8] = b"TDBTRV01";
const ATTEMPT_LOCATOR_MAGIC: &[u8; 8] = b"TDBATL01";
const ATTEMPT_LOCATOR_FIXED_BYTES: usize = 8 + 4 + 8 + 2;
const ACTIVITY_INDEX_MAGIC: &[u8; 8] = b"TDBACT01";
const TOMBSTONE_ATTEMPTS_MAGIC: &[u8; 8] = b"TDBTSA02";

pub(crate) struct ConversationIdentityRecord {
    pub turn_id: String,
    pub attempt_ids: Vec<String>,
    pub tombstone_deleted_at_ms: Option<u64>,
}

pub(crate) struct CloneTurnSummary {
    pub turn_count: usize,
    pub main_count: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct SearchProjectionTurn {
    pub turn_id: String,
    pub ordinal: u64,
    pub search_text: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct SearchProjectionPage {
    pub turns: Vec<SearchProjectionTurn>,
    pub next_cursor: Vec<u8>,
    pub complete: bool,
    pub skipped_oversized: usize,
    pub source_bytes: u64,
}

enum AttemptLocator {
    LegacyOwner(u64),
    Conversation {
        owner_user_id: u64,
        conversation_id: String,
    },
}

struct DeleteTurnRow {
    lane_id: String,
    ordinal: u64,
    updated_at_ms: u64,
    attempt_id: Option<String>,
}

struct CompactTurnMetadata {
    turn_id: String,
    parent_turn_id: Option<String>,
    status: String,
    projection_revision: u64,
    updated_at_ms: u64,
    ordinal: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum TurnConflictKind {
    InProgress,
    LaneAdvanced,
    ParentInvalid,
    ProjectionStale,
    SupersededByHuman,
}

impl fmt::Display for TurnConflictKind {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(match self {
            Self::InProgress => "turn is in progress",
            Self::LaneAdvanced => "turn lane advanced",
            Self::ParentInvalid => "turn parent is invalid",
            Self::ProjectionStale => "turn projection revision is stale",
            Self::SupersededByHuman => "turn superseded by human",
        })
    }
}

impl std::error::Error for TurnConflictKind {}

fn typed_conflict(kind: TurnConflictKind) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, kind)
}

pub(crate) fn conflict_kind(error: &io::Error) -> Option<TurnConflictKind> {
    error
        .get_ref()
        .and_then(|source| source.downcast_ref::<TurnConflictKind>())
        .copied()
}

pub(crate) struct AppendSettledRequest {
    pub conversation_id: String,
    pub actor: String,
    pub status: String,
    pub projection_json: Vec<u8>,
    pub settlement_json: Vec<u8>,
    pub lane_id: String,
    pub command_id: String,
    pub kind: String,
    pub run_id: String,
    pub turn_id: String,
    pub attempt_id: Option<String>,
    pub created_at_ms: u64,
    pub committed_at_ms: u64,
    pub defaults: TurnDefaults,
}

#[derive(Clone, Debug)]
pub struct CreatePairQueueBinding {
    pub queue_id: String,
    pub message: Map<String, Value>,
    pub kind: String,
    pub priority: u64,
    pub created_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct CreatePairRequest {
    pub conversation_id: String,
    pub command_id: String,
    pub dispatch_mode: String,
    pub lane_id: String,
    pub input_actor: String,
    pub output_actor: String,
    pub input_projection: Map<String, Value>,
    pub input_presentation_id: String,
    pub output_presentation_id: String,
    pub input_kind: String,
    pub output_kind: String,
    pub run_id: String,
    pub config: Map<String, Value>,
    pub parent_turn_id: Option<String>,
    pub require_lane_idle: bool,
    pub require_parent_is_lane_tail: bool,
    pub reject_if_human_queued: bool,
    pub input_turn_id: String,
    pub output_turn_id: String,
    pub input_attempt_id: Option<String>,
    pub output_attempt_id: String,
    pub queue_binding: Option<CreatePairQueueBinding>,
    pub created_at_ms: u64,
    pub committed_at_ms: u64,
    pub defaults: TurnDefaults,
}

#[derive(Clone, Debug)]
pub struct QueueTransitionRequest {
    pub conversation_id: String,
    pub queue_id: String,
    pub committed_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct SteerCommitRequest {
    pub conversation_id: String,
    pub attempt_id: String,
    pub command_id: String,
    pub text: String,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct RelatedAnnounceRequest {
    pub attempt_id: String,
    pub turn_ids: Vec<String>,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct VisibleSyncRequest {
    pub conversation_id: String,
    pub attempt_id: String,
    pub root_turn_id: String,
    pub messages: Vec<Map<String, Value>>,
    pub default_kind: String,
    pub run_id: String,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

pub(crate) struct ProjectionUpdateRequest {
    pub conversation_id: String,
    pub turn_id: String,
    pub projection_json: Vec<u8>,
    pub expected_projection_revision: u64,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

pub(crate) struct BranchCreateRequest {
    pub conversation_id: String,
    pub parent_turn_id: String,
    pub lane_id: String,
    pub title: String,
    pub kind: String,
    pub anchor_text: String,
    pub parent_selection: String,
    pub expected_projection_revision: u64,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

pub(crate) struct BranchDeleteRequest {
    pub conversation_id: String,
    pub parent_turn_id: String,
    pub lane_id: String,
    pub deleted_at_ms: u64,
    pub committed_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct AttemptCreateRequest {
    pub conversation_id: String,
    pub turn_id: String,
    pub attempt_id: String,
    pub command_id: String,
    pub operation: String,
    pub dispatch_mode: String,
    pub expected_projection_revision: u64,
    pub resume_anchor: Option<Map<String, Value>>,
    pub config: Value,
    pub input_update: Option<Map<String, Value>>,
    pub expected_input_projection_revision: Option<u64>,
    pub target_actor: Option<String>,
    pub target_kind: Option<String>,
    pub created_at_ms: u64,
    pub committed_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct AttemptDispatchWorkerRequest {
    pub attempt_id: String,
    pub user_id: u64,
    pub principal: Value,
    pub tenant_label: String,
    pub priority: u16,
    pub now_ms: u64,
}

#[derive(Clone, Debug)]
pub struct RecoverRequest {
    pub max_rows: usize,
    pub max_bytes: usize,
    pub created_before_ms: Option<u64>,
    pub exclude_task_ids: BTreeSet<String>,
    pub now_ms: u64,
}

#[derive(Clone, Debug)]
pub struct EventPruneRequest {
    pub settled_before_ms: u64,
    pub max_attempts: usize,
    pub max_rows: usize,
}

#[derive(Clone, Debug)]
pub struct SyncPruneRequest {
    pub created_before_ms: u64,
    pub max_rows: usize,
}

#[derive(Clone, Debug)]
pub struct CompactProjectionUpdate {
    pub turn_id: String,
    pub expected_projection_revision: u64,
    pub projection_json: Vec<u8>,
}

#[derive(Clone, Debug)]
pub struct CompactRequest {
    pub conversation_id: String,
    pub expected_conversation_revision: u64,
    pub summary_turn_id: String,
    pub summary_projection_json: Vec<u8>,
    pub delete_turn_ids: Vec<String>,
    pub projection_updates: Vec<CompactProjectionUpdate>,
    pub insert_after_turn_id: Option<String>,
    pub insert_before_turn_id: Option<String>,
    pub now_ms: u64,
}

#[derive(Clone, Debug)]
pub struct PerceptionRecordRequest {
    pub conversation_id: String,
    pub turn_id: String,
    pub attempt_id: String,
    pub observation: Map<String, Value>,
    pub recorded_at_ms: u64,
}

#[derive(Clone, Debug)]
pub struct EventRecordRequest {
    pub attempt_id: String,
    pub task_id: String,
    pub terminal: bool,
    pub status: String,
    pub slim: bool,
    pub content: String,
    pub thinking: String,
    pub projection: Value,
    pub projection_patch: Option<Map<String, Value>>,
    pub settlement: Map<String, Value>,
    pub error: Map<String, Value>,
    pub event_type: String,
    pub event_payload: Map<String, Value>,
    pub task_event: Option<crate::transaction_ir::IndexedStreamAppendItem>,
    pub now_ms: u64,
}

struct StagedProjectionUpdate {
    turn: Value,
    before: Map<String, Value>,
    after: Map<String, Value>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProjectionUpdateMode {
    TypedSettledEdit,
    InternalSettledMutation,
    FencedLiveSteer,
}

struct LoadedAttemptForUpdate {
    key: EntityKey,
    physical_version: u64,
    document: Map<String, Value>,
    conversation_id: String,
}

struct LoadedTurnForAttempt {
    key: EntityKey,
    physical_version: u64,
    document: Map<String, Value>,
    projection_head: Option<ProjectionHeadDescriptor>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ProjectionHeadDescriptor {
    head_id: String,
    attempt_id: String,
    base_revision: u64,
    patch_count: usize,
    patch_bytes: usize,
}

fn invalid_data(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn conflict(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::WouldBlock, message)
}

fn not_found(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::NotFound, message)
}

fn entity_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    logical_key: &[u8],
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        logical_key,
    )
}

fn push_text_key(output: &mut Vec<u8>, value: &str) -> io::Result<()> {
    output.extend_from_slice(
        &u16::try_from(value.len())
            .map_err(|_| invalid_input("turn identity exceeds its bound"))?
            .to_be_bytes(),
    );
    output.extend_from_slice(value.as_bytes());
    Ok(())
}

fn conversation_prefix(conversation_id: &str) -> io::Result<Vec<u8>> {
    let mut output = Vec::with_capacity(2 + conversation_id.len());
    push_text_key(&mut output, conversation_id)?;
    Ok(output)
}

pub(crate) fn conversation_recoverable_ranges(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<(EntityKey, EntityKey)>> {
    let prefix = conversation_prefix(conversation_id)?;
    [
        TURN_DOCUMENT_NAMESPACE,
        TURN_LANE_INDEX_NAMESPACE,
        TURN_LANE_COMPACTION_INDEX_NAMESPACE,
        TURN_ACTIVITY_INDEX_NAMESPACE,
        TURN_LANE_HEAD_NAMESPACE,
        TURN_LANE_COUNT_NAMESPACE,
        TURN_UPDATED_INDEX_NAMESPACE,
        TURN_TOMBSTONE_NAMESPACE,
        TURN_PROJECTION_HEAD_NAMESPACE,
        ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE,
        ATTEMPT_TURN_DIRECTORY_NAMESPACE,
        ATTEMPT_TURN_COUNT_NAMESPACE,
    ]
    .into_iter()
    .map(|namespace| {
        EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            namespace,
            &prefix,
        )
    })
    .collect()
}

pub(crate) fn conversation_executable_ranges(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<(EntityKey, EntityKey)>> {
    let prefix = conversation_prefix(conversation_id)?;
    let mut ranges = [
        GENERATION_ATTEMPT_NAMESPACE,
        ATTEMPT_COMMAND_NAMESPACE,
        CONVERSATION_SYNC_EVENT_NAMESPACE,
        TURN_LANE_LIVE_ATTEMPT_NAMESPACE,
    ]
    .into_iter()
    .map(|namespace| {
        EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            namespace,
            &prefix,
        )
    })
    .collect::<io::Result<Vec<_>>>()?;
    ranges.push(entity_key(transaction, CONVERSATION_SYNC_HEAD_NAMESPACE, &prefix)?.exact_range()?);
    Ok(ranges)
}

pub(crate) fn conversation_identity_records(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<ConversationIdentityRecord>> {
    let prefix = conversation_prefix(conversation_id)?;
    let documents = scan_conversation_identity_namespace(
        database,
        transaction,
        TURN_DOCUMENT_NAMESPACE,
        &prefix,
        MAX_DELETE_ROWS,
    )?;
    let mut records = BTreeMap::<String, ConversationIdentityRecord>::new();
    for (document_key, stored) in documents {
        let document = materialize_turn(database, transaction, &stored)?;
        if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id) {
            return Err(invalid_data(
                "conversation purge Turn identity is malformed",
            ));
        }
        let turn_id = document
            .get("turnId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("conversation purge Turn identity is malformed"))?
            .to_owned();
        let current_attempt_id = document
            .get("currentAttemptId")
            .and_then(Value::as_str)
            .map(str::to_owned);
        let attempt_ids = load_attempt_identity_ids(
            database,
            transaction,
            conversation_id,
            &turn_id,
            current_attempt_id.as_deref(),
        )?;
        if document_key != turn_key(transaction, conversation_id, &turn_id)?
            || records
                .insert(
                    turn_id.clone(),
                    ConversationIdentityRecord {
                        turn_id,
                        attempt_ids,
                        tombstone_deleted_at_ms: None,
                    },
                )
                .is_some()
        {
            return Err(invalid_data(
                "conversation purge Turn storage identity is inconsistent",
            ));
        }
    }

    let tombstones = scan_conversation_identity_namespace(
        database,
        transaction,
        TURN_TOMBSTONE_NAMESPACE,
        &prefix,
        MAX_DELETE_ROWS - records.len(),
    )?;
    for (key, attempt_id_bytes) in tombstones {
        let suffix = key
            .key_bytes()
            .get(prefix.len()..)
            .ok_or_else(|| invalid_data("conversation purge tombstone is malformed"))?;
        if suffix.len() < 9 {
            return Err(invalid_data("conversation purge tombstone is malformed"));
        }
        let deleted_at_ms = u64::from_be_bytes(suffix[..8].try_into().unwrap());
        let turn_id = decode_tombstone_turn_id(&key, prefix.len())?;
        let attempt_ids = decode_tombstone_attempt_ids(&attempt_id_bytes)?;
        if records
            .insert(
                turn_id.clone(),
                ConversationIdentityRecord {
                    turn_id,
                    attempt_ids,
                    tombstone_deleted_at_ms: Some(deleted_at_ms),
                },
            )
            .is_some()
        {
            return Err(invalid_data("conversation purge identity is duplicated"));
        }
    }
    Ok(records.into_values().collect())
}

pub(crate) fn retire_conversation_attempt_events(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<()> {
    for record in conversation_identity_records(database, transaction, conversation_id)? {
        for attempt_id in record.attempt_ids {
            database.entity_delete(
                transaction,
                attempt_event_head_key(transaction, &attempt_id)?,
            )?;
            let prefix = attempt_event_prefix(&attempt_id)?;
            let (start, end) = EntityKey::prefix_range(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                ATTEMPT_EVENT_NAMESPACE,
                &prefix,
            )?;
            database.entity_retire_range(transaction, &start, &end)?;
        }
    }
    Ok(())
}

fn scan_conversation_identity_namespace(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    prefix: &[u8],
    maximum: usize,
) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        prefix,
    )?;
    let mut rows = Vec::new();
    loop {
        let page_limit = (maximum + 1 - rows.len().min(maximum)).min(INDEX_PAGE_ROWS);
        let page = database.entity_scan(transaction, &start, &end, page_limit)?;
        if rows.len() + page.len() > maximum {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "conversation identity scan exceeds its bound",
            ));
        }
        if page.len() < page_limit {
            rows.extend(page);
            break;
        }
        start = after_key(&page.last().unwrap().0, transaction, namespace)?;
        rows.extend(page);
    }
    Ok(rows)
}

pub(crate) fn release_conversation_identity_claims(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    records: &[ConversationIdentityRecord],
) -> io::Result<()> {
    for record in records {
        delete_owned_identity_claim(
            database,
            transaction,
            TURN_ID_CLAIM_NAMESPACE,
            &record.turn_id,
        )?;
        for attempt_id in &record.attempt_ids {
            delete_owned_identity_claim(
                database,
                transaction,
                ATTEMPT_ID_CLAIM_NAMESPACE,
                attempt_id,
            )?;
        }
        if let Some(deleted_at_ms) = record.tombstone_deleted_at_ms {
            database.entity_delete(
                transaction,
                tombstone_age_index_key(
                    transaction,
                    deleted_at_ms,
                    conversation_id,
                    &record.turn_id,
                )?,
            )?;
        }
    }
    Ok(())
}

pub(crate) fn conversation_turn_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<u64> {
    let prefix = conversation_prefix(conversation_id)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_COUNT_NAMESPACE,
        &prefix,
    )?;
    let rows = database.entity_scan(transaction, &start, &end, MAX_DELETE_BRANCH_LANES + 1)?;
    if rows.len() > MAX_DELETE_BRANCH_LANES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "conversation lane count exceeds 256",
        ));
    }
    rows.into_iter().try_fold(0_u64, |total, (_, value)| {
        total
            .checked_add(decode_u64(Some(value), "turn lane count is malformed")?)
            .ok_or_else(|| invalid_data("conversation turn count overflow"))
    })
}

fn turn_logical_key(conversation_id: &str, turn_id: &str) -> String {
    format!("{conversation_id}\u{1f}{turn_id}")
}

fn turn_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, turn_id)?;
    entity_key(transaction, TURN_DOCUMENT_NAMESPACE, &encoded)
}

fn global_identity_claim_key(
    transaction: &AuthorityTransaction,
    namespace: &str,
    identity: &str,
) -> io::Result<EntityKey> {
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        namespace,
        identity.as_bytes(),
    )
}

fn attempt_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, attempt_id)?;
    entity_key(transaction, GENERATION_ATTEMPT_NAMESPACE, &encoded)
}

fn attempt_key_for_owner(
    tenant_id: u64,
    owner_user_id: u64,
    conversation_id: &str,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, attempt_id)?;
    EntityKey::new(
        tenant_id,
        owner_user_id,
        GENERATION_ATTEMPT_NAMESPACE,
        &encoded,
    )
}

fn turn_key_for_owner(
    tenant_id: u64,
    owner_user_id: u64,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, turn_id)?;
    EntityKey::new(tenant_id, owner_user_id, TURN_DOCUMENT_NAMESPACE, &encoded)
}

fn dispatchable_index_key(
    transaction: &AuthorityTransaction,
    created_at_ms: u64,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(8 + attempt_id.len());
    encoded.extend_from_slice(&created_at_ms.to_be_bytes());
    encoded.extend_from_slice(attempt_id.as_bytes());
    EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
        &encoded,
    )
}

fn dispatchable_index_value(
    owner_user_id: u64,
    conversation_id: &str,
    turn_id: &str,
    attempt_id: &str,
) -> io::Result<Vec<u8>> {
    serde_json::to_vec(&json!({
        "userId": owner_user_id,
        "conversationId": conversation_id,
        "turnId": turn_id,
        "attemptId": attempt_id,
    }))
    .map_err(|_| invalid_data("attempt dispatchable index cannot be encoded"))
}

fn recovery_index_key(
    transaction: &AuthorityTransaction,
    created_at_ms: u64,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(8 + attempt_id.len());
    encoded.extend_from_slice(&created_at_ms.to_be_bytes());
    encoded.extend_from_slice(attempt_id.as_bytes());
    entity_key(transaction, ATTEMPT_RECOVERY_INDEX_NAMESPACE, &encoded)
}

fn recovery_index_value(
    attempt: &Map<String, Value>,
    projection_bytes: usize,
) -> io::Result<Vec<u8>> {
    let text = |field: &str| {
        attempt
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt recovery identity is malformed"))
    };
    serde_json::to_vec(&json!({
        "attemptId": text("attemptId")?,
        "conversationId": text("conversationId")?,
        "turnId": text("turnId")?,
        "taskId": text("taskId")?,
        "projectionBytes": projection_bytes,
    }))
    .map_err(|_| invalid_data("attempt recovery index cannot be encoded"))
}

fn attempt_is_live(attempt: &Map<String, Value>) -> bool {
    matches!(
        attempt.get("status").and_then(Value::as_str),
        Some("pending" | "running")
    )
}

fn update_recovery_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    previous: &Map<String, Value>,
    next: &Map<String, Value>,
) -> io::Result<()> {
    let created_at_ms = previous
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
    let attempt_id = previous
        .get("attemptId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt recovery identity is malformed"))?;
    let key = recovery_index_key(transaction, created_at_ms, attempt_id)?;
    match (attempt_is_live(previous), attempt_is_live(next)) {
        (true, true) => {
            let existing = database
                .entity_get(transaction, &key)?
                .ok_or_else(|| invalid_data("live attempt recovery index is missing"))?;
            let projection_bytes = serde_json::from_slice::<Value>(&existing)
                .ok()
                .and_then(|value| value.get("projectionBytes").and_then(Value::as_u64))
                .and_then(|value| usize::try_from(value).ok())
                .ok_or_else(|| invalid_data("attempt recovery byte evidence is malformed"))?;
            database.entity_put(
                transaction,
                key,
                recovery_index_value(next, projection_bytes)?,
            )
        }
        (true, false) => database.entity_delete(transaction, key),
        (false, true) => Err(invalid_data("terminal attempt cannot become live")),
        (false, false) => Ok(()),
    }
}

fn push_descending_text_key(output: &mut Vec<u8>, value: &str) {
    for byte in value.as_bytes() {
        output.extend_from_slice(&[!byte, 0]);
    }
    output.push(u8::MAX);
}

fn attempt_timing_effective_at(attempt: &Map<String, Value>) -> io::Result<u64> {
    for field in ["settledAt", "startedAt", "createdAt"] {
        match attempt.get(field) {
            Some(Value::Null) | None => {}
            Some(Value::Number(value)) => {
                return value
                    .as_u64()
                    .ok_or_else(|| invalid_data("attempt timing timestamp is malformed"));
            }
            Some(_) => return Err(invalid_data("attempt timing timestamp is malformed")),
        }
    }
    Err(invalid_data("attempt timing timestamp is missing"))
}

fn attempt_timing_task_index_key(
    transaction: &AuthorityTransaction,
    task_id: &str,
    effective_at: u64,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(12 + task_id.len() + attempt_id.len() * 2);
    push_text_key(&mut encoded, task_id)?;
    encoded.extend_from_slice(&(!effective_at).to_be_bytes());
    push_descending_text_key(&mut encoded, attempt_id);
    entity_key(transaction, ATTEMPT_TIMING_TASK_INDEX_NAMESPACE, &encoded)
}

fn attempt_timing_conversation_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    created_at: u64,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    encoded.extend_from_slice(&(!created_at).to_be_bytes());
    push_descending_text_key(&mut encoded, attempt_id);
    entity_key(
        transaction,
        ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE,
        &encoded,
    )
}

fn attempt_timing_index_value(attempt: &Map<String, Value>) -> io::Result<Vec<u8>> {
    let text = |field: &str| {
        attempt
            .get(field)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("attempt timing index identity is malformed"))
    };
    let created_at = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt timing creation timestamp is malformed"))?;
    let settled_at = match attempt.get("settledAt") {
        None | Some(Value::Null) => Value::Null,
        Some(Value::Number(value)) if value.as_u64().is_some() => Value::Number(value.clone()),
        Some(_) => {
            return Err(invalid_data(
                "attempt timing settlement timestamp is malformed",
            ))
        }
    };
    serde_json::to_vec(&json!({
        "attemptId": text("attemptId")?,
        "conversationId": text("conversationId")?,
        "turnId": text("turnId")?,
        "taskId": text("taskId")?,
        "status": text("status")?,
        "createdAt": created_at,
        "settledAt": settled_at,
        "effectiveAt": attempt_timing_effective_at(attempt)?,
    }))
    .map_err(|_| invalid_data("attempt timing index cannot be encoded"))
}

fn remove_attempt_timing_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt: &Map<String, Value>,
) -> io::Result<()> {
    let Some(task_id) = attempt
        .get("taskId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return Ok(());
    };
    let attempt_id = attempt
        .get("attemptId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt timing identity is malformed"))?;
    let conversation_id = attempt
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt timing conversation identity is malformed"))?;
    let created_at = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt timing creation timestamp is malformed"))?;
    let effective_at = attempt_timing_effective_at(attempt)?;
    database.entity_delete(
        transaction,
        attempt_timing_task_index_key(transaction, task_id, effective_at, attempt_id)?,
    )?;
    database.entity_delete(
        transaction,
        attempt_timing_conversation_index_key(
            transaction,
            conversation_id,
            created_at,
            attempt_id,
        )?,
    )
}

fn put_attempt_timing_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt: &Map<String, Value>,
) -> io::Result<()> {
    let Some(task_id) = attempt
        .get("taskId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    else {
        return Ok(());
    };
    let attempt_id = attempt
        .get("attemptId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt timing identity is malformed"))?;
    let conversation_id = attempt
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt timing conversation identity is malformed"))?;
    let created_at = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt timing creation timestamp is malformed"))?;
    let effective_at = attempt_timing_effective_at(attempt)?;
    let value = attempt_timing_index_value(attempt)?;
    database.entity_put(
        transaction,
        attempt_timing_task_index_key(transaction, task_id, effective_at, attempt_id)?,
        value.clone(),
    )?;
    database.entity_put(
        transaction,
        attempt_timing_conversation_index_key(
            transaction,
            conversation_id,
            created_at,
            attempt_id,
        )?,
        value,
    )
}

fn update_attempt_timing_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    previous: &Map<String, Value>,
    next: &Map<String, Value>,
) -> io::Result<()> {
    remove_attempt_timing_indexes(database, transaction, previous)?;
    put_attempt_timing_indexes(database, transaction, next)
}

fn remove_directory_entry_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    entry: &Map<String, Value>,
) -> io::Result<()> {
    if entry.get("identityOnly").and_then(Value::as_bool) == Some(true) {
        return Ok(());
    }
    let attempt_id = directory_entry_text(entry, "attemptId")?;
    let created_at = entry
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt Turn directory timestamp is malformed"))?;
    if entry.get("dispatchMode").and_then(Value::as_str) == Some("conversation_executor") {
        database.entity_delete(
            transaction,
            dispatchable_index_key(transaction, created_at, attempt_id)?,
        )?;
    }
    let effective_at = entry
        .get("effectiveAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt Turn directory timestamp is malformed"))?;
    database.entity_delete(
        transaction,
        attempt_event_retention_index_key(transaction, effective_at, attempt_id)?,
    )?;
    let task_id = directory_entry_text(entry, "taskId")?;
    if task_id.is_empty() {
        return Ok(());
    }
    database.entity_delete(
        transaction,
        attempt_timing_task_index_key(transaction, task_id, effective_at, attempt_id)?,
    )?;
    database.entity_delete(
        transaction,
        attempt_timing_conversation_index_key(
            transaction,
            conversation_id,
            created_at,
            attempt_id,
        )?,
    )
}

fn remove_dispatchable_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt: &Map<String, Value>,
) -> io::Result<()> {
    if attempt.get("_dispatchMode").and_then(Value::as_str) != Some("conversation_executor") {
        return Ok(());
    }
    let created_at_ms = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt creation timestamp is malformed"))?;
    let attempt_id = attempt
        .get("attemptId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt identity is malformed"))?;
    database.entity_delete(
        transaction,
        dispatchable_index_key(transaction, created_at_ms, attempt_id)?,
    )
}

fn legacy_attempt_key(
    transaction: &AuthorityTransaction,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    entity_key(
        transaction,
        GENERATION_ATTEMPT_NAMESPACE,
        attempt_id.as_bytes(),
    )
}

fn attempt_event_head_key(
    transaction: &AuthorityTransaction,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    entity_key(
        transaction,
        ATTEMPT_EVENT_HEAD_NAMESPACE,
        attempt_id.as_bytes(),
    )
}

fn attempt_event_key(
    transaction: &AuthorityTransaction,
    attempt_id: &str,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(attempt_id.len() + 10);
    push_text_key(&mut encoded, attempt_id)?;
    encoded.extend_from_slice(&sequence.to_be_bytes());
    entity_key(transaction, ATTEMPT_EVENT_NAMESPACE, &encoded)
}

fn attempt_event_prefix(attempt_id: &str) -> io::Result<Vec<u8>> {
    let mut encoded = Vec::with_capacity(attempt_id.len() + 2);
    push_text_key(&mut encoded, attempt_id)?;
    Ok(encoded)
}

fn attempt_event_retention_index_key(
    transaction: &AuthorityTransaction,
    settled_at_ms: u64,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(10 + attempt_id.len());
    encoded.extend_from_slice(&settled_at_ms.to_be_bytes());
    push_text_key(&mut encoded, attempt_id)?;
    entity_key(
        transaction,
        ATTEMPT_EVENT_RETENTION_INDEX_NAMESPACE,
        &encoded,
    )
}

fn terminal_attempt_settled_at(attempt: &Map<String, Value>) -> io::Result<Option<u64>> {
    let status = attempt
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt retention status is malformed"))?;
    if matches!(status, "pending" | "running") {
        return Ok(None);
    }
    attempt
        .get("settledAt")
        .and_then(Value::as_u64)
        .map(Some)
        .ok_or_else(|| invalid_data("terminal attempt settlement timestamp is malformed"))
}

fn attempt_event_retention_index_value(
    attempt: &Map<String, Value>,
    next_sequence: u64,
) -> io::Result<Vec<u8>> {
    let text = |field: &str| {
        attempt
            .get(field)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("attempt retention identity is malformed"))
    };
    let settled_at_ms = terminal_attempt_settled_at(attempt)?
        .ok_or_else(|| invalid_data("live attempt cannot enter event retention"))?;
    serde_json::to_vec(&json!({
        "attemptId": text("attemptId")?,
        "conversationId": text("conversationId")?,
        "turnId": text("turnId")?,
        "status": text("status")?,
        "settledAt": settled_at_ms,
        "nextSequence": next_sequence,
    }))
    .map_err(|_| invalid_data("attempt retention index cannot be encoded"))
}

fn update_attempt_event_retention_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    previous: &Map<String, Value>,
    next: &Map<String, Value>,
) -> io::Result<()> {
    let previous_settled = terminal_attempt_settled_at(previous)?;
    let next_settled = terminal_attempt_settled_at(next)?;
    let previous_attempt_id = previous
        .get("attemptId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt retention identity is malformed"))?;
    let next_attempt_id = next
        .get("attemptId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt retention identity is malformed"))?;
    if previous_attempt_id != next_attempt_id {
        return Err(invalid_data("attempt retention identity changed"));
    }
    if let Some(settled_at_ms) = previous_settled {
        let previous_key =
            attempt_event_retention_index_key(transaction, settled_at_ms, previous_attempt_id)?;
        if next_settled != Some(settled_at_ms) {
            database.entity_delete(transaction, previous_key)?;
        } else if let Some(existing) = database.entity_get(transaction, &previous_key)? {
            let next_sequence = serde_json::from_slice::<Value>(&existing)
                .ok()
                .and_then(|value| value.get("nextSequence").and_then(Value::as_u64))
                .filter(|sequence| *sequence > 0)
                .ok_or_else(|| invalid_data("attempt retention cursor is malformed"))?;
            return database.entity_put(
                transaction,
                previous_key,
                attempt_event_retention_index_value(next, next_sequence)?,
            );
        } else {
            // A missing terminal marker means its stream was already drained.
            return Ok(());
        }
    }
    if let Some(settled_at_ms) = next_settled {
        database.entity_put(
            transaction,
            attempt_event_retention_index_key(transaction, settled_at_ms, next_attempt_id)?,
            attempt_event_retention_index_value(next, 1)?,
        )?;
    }
    Ok(())
}

fn attempt_command_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    command_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, command_id)?;
    entity_key(transaction, ATTEMPT_COMMAND_NAMESPACE, &encoded)
}

fn attempt_turn_count_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, turn_id)?;
    entity_key(transaction, ATTEMPT_TURN_COUNT_NAMESPACE, &encoded)
}

fn attempt_turn_directory_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, turn_id)?;
    entity_key(transaction, ATTEMPT_TURN_DIRECTORY_NAMESPACE, &encoded)
}

fn directory_entry_text<'a>(entry: &'a Map<String, Value>, field: &str) -> io::Result<&'a str> {
    entry
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt Turn directory identity is malformed"))
}

fn attempt_turn_directory_entry(attempt: &Map<String, Value>) -> io::Result<Map<String, Value>> {
    let attempt_id = directory_entry_text(attempt, "attemptId")?;
    let conversation_id = directory_entry_text(attempt, "conversationId")?;
    let turn_id = directory_entry_text(attempt, "turnId")?;
    let task_id = directory_entry_text(attempt, "taskId")?;
    let created_at = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("attempt Turn directory timestamp is malformed"))?;
    Ok(Map::from_iter([
        ("attemptId".to_owned(), Value::String(attempt_id.to_owned())),
        (
            "conversationId".to_owned(),
            Value::String(conversation_id.to_owned()),
        ),
        ("turnId".to_owned(), Value::String(turn_id.to_owned())),
        ("taskId".to_owned(), Value::String(task_id.to_owned())),
        ("createdAt".to_owned(), Value::from(created_at)),
        (
            "effectiveAt".to_owned(),
            Value::from(attempt_timing_effective_at(attempt)?),
        ),
        (
            "dispatchMode".to_owned(),
            Value::String(
                attempt
                    .get("_dispatchMode")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_owned(),
            ),
        ),
    ]))
}

fn decode_attempt_turn_directory(raw: &[u8]) -> io::Result<Vec<Map<String, Value>>> {
    if raw.len() > MAX_ATTEMPT_TURN_DIRECTORY_BYTES {
        return Err(invalid_data("attempt Turn directory exceeds 64 KiB"));
    }
    let entries = serde_json::from_slice::<Value>(raw)
        .ok()
        .and_then(|value| value.as_array().cloned())
        .ok_or_else(|| invalid_data("attempt Turn directory is malformed"))?;
    if entries.len() > MAX_ATTEMPTS_PER_TURN {
        return Err(invalid_data("attempt Turn directory exceeds 64 entries"));
    }
    let mut seen = BTreeSet::new();
    entries
        .into_iter()
        .map(|entry| {
            let entry = entry
                .as_object()
                .cloned()
                .ok_or_else(|| invalid_data("attempt Turn directory entry is malformed"))?;
            let attempt_id = directory_entry_text(&entry, "attemptId")?;
            if attempt_id.is_empty()
                || attempt_id.chars().count() > 128
                || !seen.insert(attempt_id.to_owned())
                || directory_entry_text(&entry, "conversationId")?.is_empty()
                || directory_entry_text(&entry, "conversationId")?
                    .chars()
                    .count()
                    > 256
                || directory_entry_text(&entry, "turnId")?.is_empty()
                || directory_entry_text(&entry, "turnId")?.chars().count() > 128
                || directory_entry_text(&entry, "taskId")?.chars().count() > 256
                || entry.get("createdAt").and_then(Value::as_u64).is_none()
                || entry.get("effectiveAt").and_then(Value::as_u64).is_none()
                || entry.get("dispatchMode").and_then(Value::as_str).is_none()
            {
                return Err(invalid_data("attempt Turn directory entry is malformed"));
            }
            Ok(entry)
        })
        .collect()
}

fn load_attempt_identity_ids(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    legacy_current_attempt_id: Option<&str>,
) -> io::Result<Vec<String>> {
    let stored_count = database.entity_get(
        transaction,
        &attempt_turn_count_key(transaction, conversation_id, turn_id)?,
    )?;
    let expected_count = if stored_count.is_none() && legacy_current_attempt_id.is_some() {
        1
    } else {
        decode_u64(stored_count, "turn attempt count is malformed")?
    };
    let directory_key = attempt_turn_directory_key(transaction, conversation_id, turn_id)?;
    let Some(raw) = database.entity_get(transaction, &directory_key)? else {
        return match (expected_count, legacy_current_attempt_id) {
            (0, None) => Ok(Vec::new()),
            (1, Some(attempt_id)) => Ok(vec![attempt_id.to_owned()]),
            _ => Err(invalid_data("legacy turn attempt directory is incomplete")),
        };
    };
    let entries = decode_attempt_turn_directory(&raw)?;
    if entries.len() as u64 != expected_count
        || entries.iter().any(|entry| {
            directory_entry_text(entry, "conversationId").ok() != Some(conversation_id)
                || directory_entry_text(entry, "turnId").ok() != Some(turn_id)
        })
    {
        return Err(invalid_data("turn attempt directory count is inconsistent"));
    }
    entries
        .into_iter()
        .map(|entry| directory_entry_text(&entry, "attemptId").map(str::to_owned))
        .collect()
}

fn load_attempt_turn_directory(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    legacy_current_attempt_id: Option<&str>,
) -> io::Result<Vec<Map<String, Value>>> {
    let key = attempt_turn_directory_key(transaction, conversation_id, turn_id)?;
    let stored_count = database.entity_get(
        transaction,
        &attempt_turn_count_key(transaction, conversation_id, turn_id)?,
    )?;
    let expected_count = if stored_count.is_none() && legacy_current_attempt_id.is_some() {
        1
    } else {
        decode_u64(stored_count, "turn attempt count is malformed")?
    };
    if let Some(raw) = database.entity_get(transaction, &key)? {
        let entries = decode_attempt_turn_directory(&raw)?;
        if entries.len() as u64 != expected_count
            || entries.iter().any(|entry| {
                directory_entry_text(entry, "conversationId").ok() != Some(conversation_id)
                    || directory_entry_text(entry, "turnId").ok() != Some(turn_id)
            })
        {
            return Err(invalid_data("turn attempt directory count is inconsistent"));
        }
        return Ok(entries);
    }
    let Some(attempt_id) = legacy_current_attempt_id else {
        if expected_count != 0 {
            return Err(invalid_data("turn attempt directory is missing"));
        }
        return Ok(Vec::new());
    };
    if expected_count != 1 {
        return Err(invalid_data("legacy turn attempt directory is incomplete"));
    }
    let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? else {
        if crate::conversation_header::execution_epoch(database, transaction, conversation_id)? == 0
        {
            return Err(invalid_data("legacy current attempt is missing"));
        }
        return Ok(vec![Map::from_iter([
            ("attemptId".to_owned(), Value::String(attempt_id.to_owned())),
            (
                "conversationId".to_owned(),
                Value::String(conversation_id.to_owned()),
            ),
            ("turnId".to_owned(), Value::String(turn_id.to_owned())),
            ("taskId".to_owned(), Value::String(String::new())),
            ("createdAt".to_owned(), Value::from(0)),
            ("effectiveAt".to_owned(), Value::from(0)),
            ("dispatchMode".to_owned(), Value::String(String::new())),
            ("identityOnly".to_owned(), Value::Bool(true)),
        ])]);
    };
    if loaded.conversation_id != conversation_id
        || loaded.document.get("turnId").and_then(Value::as_str) != Some(turn_id)
    {
        return Err(invalid_data("legacy current attempt target is malformed"));
    }
    Ok(vec![attempt_turn_directory_entry(&loaded.document)?])
}

fn store_attempt_turn_directory(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    entries: &[Map<String, Value>],
) -> io::Result<()> {
    if entries.len() > MAX_ATTEMPTS_PER_TURN {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn attempt history exceeds 64 entries",
        ));
    }
    let key = attempt_turn_directory_key(transaction, conversation_id, turn_id)?;
    if entries.is_empty() {
        return database.entity_delete(transaction, key);
    }
    let encoded = serde_json::to_vec(entries)
        .map_err(|_| invalid_data("attempt Turn directory cannot be encoded"))?;
    if encoded.len() > MAX_ATTEMPT_TURN_DIRECTORY_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "attempt Turn directory exceeds 64 KiB",
        ));
    }
    database.entity_put(transaction, key, encoded)
}

fn append_attempt_turn_directory(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    legacy_current_attempt_id: Option<&str>,
    attempt: &Map<String, Value>,
) -> io::Result<()> {
    let mut entries = load_attempt_turn_directory(
        database,
        transaction,
        conversation_id,
        turn_id,
        legacy_current_attempt_id,
    )?;
    let next = attempt_turn_directory_entry(attempt)?;
    let attempt_id = directory_entry_text(&next, "attemptId")?;
    if entries
        .iter()
        .any(|entry| directory_entry_text(entry, "attemptId").ok() == Some(attempt_id))
    {
        return Err(invalid_data(
            "attempt Turn directory identity is duplicated",
        ));
    }
    entries.push(next);
    store_attempt_turn_directory(database, transaction, conversation_id, turn_id, &entries)
}

fn update_attempt_turn_directory(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt: &Map<String, Value>,
) -> io::Result<()> {
    let conversation_id = directory_entry_text(attempt, "conversationId")?;
    let turn_id = directory_entry_text(attempt, "turnId")?;
    let attempt_id = directory_entry_text(attempt, "attemptId")?;
    let mut entries = load_attempt_turn_directory(
        database,
        transaction,
        conversation_id,
        turn_id,
        Some(attempt_id),
    )?;
    let position = entries
        .iter()
        .position(|entry| directory_entry_text(entry, "attemptId").ok() == Some(attempt_id))
        .ok_or_else(|| invalid_data("attempt Turn directory target is missing"))?;
    entries[position] = attempt_turn_directory_entry(attempt)?;
    store_attempt_turn_directory(database, transaction, conversation_id, turn_id, &entries)
}

fn encode_tombstone_attempt_ids(attempt_ids: &[String]) -> io::Result<Vec<u8>> {
    if attempt_ids.len() > MAX_ATTEMPTS_PER_TURN {
        return Err(invalid_data(
            "turn tombstone attempt list exceeds 64 entries",
        ));
    }
    let mut encoded = Vec::with_capacity(10 + attempt_ids.iter().map(String::len).sum::<usize>());
    encoded.extend_from_slice(TOMBSTONE_ATTEMPTS_MAGIC);
    encoded.extend_from_slice(&(attempt_ids.len() as u16).to_le_bytes());
    for attempt_id in attempt_ids {
        push_text_key(&mut encoded, attempt_id)?;
    }
    if encoded.len() > MAX_ATTEMPT_TOMBSTONE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn tombstone attempt list exceeds 16 KiB",
        ));
    }
    Ok(encoded)
}

fn decode_tombstone_attempt_ids(raw: &[u8]) -> io::Result<Vec<String>> {
    if raw.len() > MAX_ATTEMPT_TOMBSTONE_BYTES {
        return Err(invalid_data("turn tombstone attempt list exceeds 16 KiB"));
    }
    if raw.is_empty() {
        return Ok(Vec::new());
    }
    if raw.len() < 10 || &raw[..8] != TOMBSTONE_ATTEMPTS_MAGIC {
        return std::str::from_utf8(raw)
            .map(|attempt_id| vec![attempt_id.to_owned()])
            .map_err(|_| invalid_data("legacy tombstone attempt identity is not UTF-8"));
    }
    let count = u16::from_le_bytes(raw[8..10].try_into().unwrap()) as usize;
    if count > MAX_ATTEMPTS_PER_TURN {
        return Err(invalid_data(
            "turn tombstone attempt list exceeds 64 entries",
        ));
    }
    let mut offset = 10_usize;
    let mut attempt_ids = Vec::with_capacity(count);
    let mut seen = BTreeSet::new();
    for _ in 0..count {
        let length_end = offset
            .checked_add(2)
            .filter(|end| *end <= raw.len())
            .ok_or_else(|| invalid_data("turn tombstone attempt list is malformed"))?;
        let length = u16::from_be_bytes(raw[offset..length_end].try_into().unwrap()) as usize;
        offset = length_end;
        let value_end = offset
            .checked_add(length)
            .filter(|end| *end <= raw.len())
            .ok_or_else(|| invalid_data("turn tombstone attempt list is malformed"))?;
        let attempt_id = std::str::from_utf8(&raw[offset..value_end])
            .map_err(|_| invalid_data("turn tombstone attempt identity is not UTF-8"))?
            .to_owned();
        if attempt_id.is_empty()
            || attempt_id.chars().count() > 128
            || !seen.insert(attempt_id.clone())
        {
            return Err(invalid_data("turn tombstone attempt identity is malformed"));
        }
        attempt_ids.push(attempt_id);
        offset = value_end;
    }
    if offset != raw.len() {
        return Err(invalid_data(
            "turn tombstone attempt list has trailing bytes",
        ));
    }
    Ok(attempt_ids)
}

pub(crate) fn encode_attempt_locator(
    owner_user_id: u64,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    if owner_user_id == 0 || conversation_id.is_empty() {
        return Err(invalid_input("invalid attempt claim locator identity"));
    }
    let conversation_bytes = conversation_id.as_bytes();
    let conversation_length = u16::try_from(conversation_bytes.len())
        .map_err(|_| invalid_input("attempt conversation identity exceeds its bound"))?;
    let mut encoded = Vec::with_capacity(ATTEMPT_LOCATOR_FIXED_BYTES + conversation_bytes.len());
    encoded.extend_from_slice(ATTEMPT_LOCATOR_MAGIC);
    encoded.extend_from_slice(&ATTEMPT_CLAIM_LOCATOR_VERSION.to_le_bytes());
    encoded.extend_from_slice(&owner_user_id.to_be_bytes());
    encoded.extend_from_slice(&conversation_length.to_le_bytes());
    encoded.extend_from_slice(conversation_bytes);
    Ok(encoded)
}

fn decode_attempt_locator(encoded: &[u8]) -> io::Result<AttemptLocator> {
    if encoded.len() == 8 {
        let owner_user_id = u64::from_be_bytes(encoded.try_into().unwrap());
        if owner_user_id == 0 {
            return Err(invalid_data("legacy attempt claim owner is invalid"));
        }
        return Ok(AttemptLocator::LegacyOwner(owner_user_id));
    }
    if encoded.len() < ATTEMPT_LOCATOR_FIXED_BYTES
        || &encoded[..8] != ATTEMPT_LOCATOR_MAGIC
        || u32::from_le_bytes(encoded[8..12].try_into().unwrap()) != ATTEMPT_CLAIM_LOCATOR_VERSION
    {
        return Err(invalid_data("attempt claim locator is malformed"));
    }
    let owner_user_id = u64::from_be_bytes(encoded[12..20].try_into().unwrap());
    let conversation_length = u16::from_le_bytes(encoded[20..22].try_into().unwrap()) as usize;
    if owner_user_id == 0 || encoded.len() != ATTEMPT_LOCATOR_FIXED_BYTES + conversation_length {
        return Err(invalid_data("attempt claim locator identity is malformed"));
    }
    let conversation_id = std::str::from_utf8(&encoded[22..])
        .map_err(|_| invalid_data("attempt claim conversation identity is not UTF-8"))?
        .to_owned();
    if conversation_id.is_empty() {
        return Err(invalid_data("attempt claim conversation identity is empty"));
    }
    Ok(AttemptLocator::Conversation {
        owner_user_id,
        conversation_id,
    })
}

fn lane_prefix(conversation_id: &str, lane_id: &str) -> io::Result<Vec<u8>> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, lane_id)?;
    Ok(encoded)
}

fn lane_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    ordinal: u64,
) -> io::Result<EntityKey> {
    let mut encoded = lane_prefix(conversation_id, lane_id)?;
    encoded.extend_from_slice(&ordinal.to_be_bytes());
    entity_key(transaction, TURN_LANE_INDEX_NAMESPACE, &encoded)
}

fn lane_compaction_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    ordinal: u64,
) -> io::Result<EntityKey> {
    let mut encoded = lane_prefix(conversation_id, lane_id)?;
    encoded.extend_from_slice(&ordinal.to_be_bytes());
    entity_key(transaction, TURN_LANE_COMPACTION_INDEX_NAMESPACE, &encoded)
}

fn lane_compaction_index_value(document: &Map<String, Value>) -> io::Result<Vec<u8>> {
    let text = |field: &str| {
        document
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("Turn compaction index identity is malformed"))
    };
    let optional_text = |field: &str| match document.get(field) {
        None | Some(Value::Null) => Ok(Value::Null),
        Some(Value::String(value)) => Ok(Value::String(value.clone())),
        Some(_) => Err(invalid_data("Turn compaction index identity is malformed")),
    };
    let unsigned = |field: &str| {
        document
            .get(field)
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("Turn compaction index counter is malformed"))
    };
    serde_json::to_vec(&json!({
        "turnId": text("turnId")?,
        "parentTurnId": optional_text("parentTurnId")?,
        "status": text("status")?,
        "currentAttemptId": optional_text("currentAttemptId")?,
        "projectionRevision": unsigned("projectionRevision")?,
        "updatedAt": unsigned("updatedAt")?,
    }))
    .map_err(|_| invalid_data("Turn compaction index cannot be encoded"))
}

fn put_lane_compaction_index(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
) -> io::Result<()> {
    let conversation_id = document
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn conversation identity is malformed"))?;
    let lane_id = document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn lane identity is malformed"))?;
    let ordinal = document
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Turn ordinal is malformed"))?;
    database.entity_put(
        transaction,
        lane_compaction_index_key(transaction, conversation_id, lane_id, ordinal)?,
        lane_compaction_index_value(document)?,
    )
}

fn scan_lane_compaction_metadata(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<Vec<CompactTurnMetadata>> {
    let prefix = lane_prefix(conversation_id, lane_id)?;
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_COMPACTION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut rows = Vec::new();
    while rows.len() <= MAX_TURN_COMPACTION_METADATA_ROWS {
        let limit = (MAX_TURN_COMPACTION_METADATA_ROWS + 1 - rows.len()).min(INDEX_PAGE_ROWS);
        let page = database.entity_scan(transaction, &start, &end, limit)?;
        if page.is_empty() {
            break;
        }
        let page_len = page.len();
        start = after_key(
            &page.last().expect("nonempty compaction metadata page").0,
            transaction,
            TURN_LANE_COMPACTION_INDEX_NAMESPACE,
        )?;
        rows.extend(page);
        if page_len < limit {
            break;
        }
    }
    if rows.len() > MAX_TURN_COMPACTION_METADATA_ROWS {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "Turn compaction metadata exceeds 100000 rows",
        ));
    }
    let expected_count = decode_u64(
        database.entity_get(
            transaction,
            &lane_count_key(transaction, conversation_id, lane_id)?,
        )?,
        "Turn lane count is malformed",
    )?;
    if expected_count != rows.len() as u64 {
        return Err(invalid_data("Turn compaction metadata count differs"));
    }
    rows.into_iter()
        .map(|(key, encoded)| {
            let value = serde_json::from_slice::<Value>(&encoded)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("Turn compaction metadata is malformed"))?;
            let text = |field: &str| {
                value
                    .get(field)
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("Turn compaction metadata identity is malformed"))
            };
            let optional_text = |field: &str| match value.get(field) {
                None | Some(Value::Null) => Ok(None),
                Some(Value::String(value)) => Ok(Some(value.clone())),
                Some(_) => Err(invalid_data(
                    "Turn compaction metadata identity is malformed",
                )),
            };
            let ordinal_offset = prefix.len();
            let ordinal = u64::from_be_bytes(
                key.key_bytes()
                    .get(ordinal_offset..ordinal_offset + 8)
                    .filter(|_| key.key_bytes().len() == ordinal_offset + 8)
                    .ok_or_else(|| invalid_data("Turn compaction metadata key is malformed"))?
                    .try_into()
                    .unwrap(),
            );
            let metadata = CompactTurnMetadata {
                turn_id: text("turnId")?.to_owned(),
                parent_turn_id: optional_text("parentTurnId")?,
                status: text("status")?.to_owned(),
                projection_revision: value
                    .get("projectionRevision")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("Turn compaction revision is malformed"))?,
                updated_at_ms: value
                    .get("updatedAt")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("Turn compaction timestamp is malformed"))?,
                ordinal,
            };
            if key
                != lane_compaction_index_key(
                    transaction,
                    conversation_id,
                    lane_id,
                    metadata.ordinal,
                )?
            {
                return Err(invalid_data("Turn compaction metadata key differs"));
            }
            Ok(metadata)
        })
        .collect()
}

fn validate_lane_compaction_metadata_target(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    metadata: &CompactTurnMetadata,
) -> io::Result<()> {
    let compact_key =
        lane_compaction_index_key(transaction, conversation_id, lane_id, metadata.ordinal)?;
    let indexed = database
        .entity_get(transaction, &compact_key)?
        .ok_or_else(|| invalid_data("Turn compaction metadata target is missing"))?;
    let stored = database
        .entity_get(
            transaction,
            &turn_key(transaction, conversation_id, &metadata.turn_id)?,
        )?
        .ok_or_else(|| invalid_data("Turn compaction target is missing"))?;
    let document = materialize_turn(database, transaction, &stored)?;
    if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
        || document.get("laneId").and_then(Value::as_str) != Some(lane_id)
        || document.get("ordinal").and_then(Value::as_u64) != Some(metadata.ordinal)
        || lane_compaction_index_value(&document)? != indexed
    {
        return Err(invalid_data("Turn compaction metadata target differs"));
    }
    Ok(())
}

fn activity_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    ordinal: u64,
) -> io::Result<EntityKey> {
    let mut encoded = lane_prefix(conversation_id, "main")?;
    encoded.extend_from_slice(&ordinal.to_be_bytes());
    entity_key(transaction, TURN_ACTIVITY_INDEX_NAMESPACE, &encoded)
}

fn encode_activity_index_value(timestamp_ms: i128) -> Vec<u8> {
    let mut encoded = Vec::with_capacity(24);
    encoded.extend_from_slice(ACTIVITY_INDEX_MAGIC);
    encoded.extend_from_slice(&timestamp_ms.to_be_bytes());
    encoded
}

fn decode_activity_index_value(encoded: &[u8]) -> io::Result<i128> {
    if encoded.len() != 24 || &encoded[..8] != ACTIVITY_INDEX_MAGIC {
        return Err(invalid_data("Turn activity timestamp index is malformed"));
    }
    Ok(i128::from_be_bytes(encoded[8..].try_into().unwrap()))
}

fn effective_activity_timestamp(projection_timestamp: Option<&Value>, created_at_ms: u64) -> i128 {
    let projected = tolerant_activity_timestamp(projection_timestamp);
    if projected == 0 {
        i128::from(created_at_ms)
    } else {
        projected
    }
}

fn lane_count_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<EntityKey> {
    entity_key(
        transaction,
        TURN_LANE_COUNT_NAMESPACE,
        &lane_prefix(conversation_id, lane_id)?,
    )
}

fn lane_live_attempt_prefix(conversation_id: &str, lane_id: &str) -> io::Result<Vec<u8>> {
    lane_prefix(conversation_id, lane_id)
}

fn lane_live_attempt_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = lane_live_attempt_prefix(conversation_id, lane_id)?;
    push_text_key(&mut encoded, attempt_id)?;
    entity_key(transaction, TURN_LANE_LIVE_ATTEMPT_NAMESPACE, &encoded)
}

fn lane_has_live_attempt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<bool> {
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_LIVE_ATTEMPT_NAMESPACE,
        &lane_live_attempt_prefix(conversation_id, lane_id)?,
    )?;
    Ok(!database
        .entity_scan(transaction, &start, &end, 1)?
        .is_empty())
}

fn set_lane_live_attempt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    turn_id: &str,
    attempt_id: &str,
    live: bool,
) -> io::Result<()> {
    let key = lane_live_attempt_key(transaction, conversation_id, lane_id, attempt_id)?;
    if live {
        database.entity_put(transaction, key, turn_id.as_bytes().to_vec())
    } else {
        database.entity_delete(transaction, key)
    }
}

fn updated_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    updated_at_ms: u64,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    encoded.extend_from_slice(&updated_at_ms.to_be_bytes());
    push_text_key(&mut encoded, turn_id)?;
    entity_key(transaction, TURN_UPDATED_INDEX_NAMESPACE, &encoded)
}

fn tombstone_index_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    deleted_at_ms: u64,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    encoded.extend_from_slice(&deleted_at_ms.to_be_bytes());
    encoded.extend_from_slice(turn_id.as_bytes());
    entity_key(transaction, TURN_TOMBSTONE_NAMESPACE, &encoded)
}

fn tombstone_age_index_key(
    transaction: &AuthorityTransaction,
    deleted_at_ms: u64,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(12 + conversation_id.len() + turn_id.len());
    encoded.extend_from_slice(&deleted_at_ms.to_be_bytes());
    push_text_key(&mut encoded, conversation_id)?;
    push_text_key(&mut encoded, turn_id)?;
    entity_key(transaction, TURN_TOMBSTONE_AGE_INDEX_NAMESPACE, &encoded)
}

fn encode_updated_index_value(turn_id: &str, projection_revision: u64) -> io::Result<Vec<u8>> {
    let mut encoded = Vec::with_capacity(18 + turn_id.len());
    encoded.extend_from_slice(UPDATED_INDEX_MAGIC);
    encoded.extend_from_slice(&projection_revision.to_le_bytes());
    push_text_key(&mut encoded, turn_id)?;
    Ok(encoded)
}

fn decode_updated_index_value(value: &[u8]) -> io::Result<(String, u64)> {
    if value.len() < 18 || &value[..8] != UPDATED_INDEX_MAGIC {
        return Err(invalid_data("turn updated index is malformed"));
    }
    let projection_revision = u64::from_le_bytes(value[8..16].try_into().unwrap());
    let turn_id_bytes = u16::from_be_bytes(value[16..18].try_into().unwrap()) as usize;
    if projection_revision == 0 || value.len() != 18 + turn_id_bytes {
        return Err(invalid_data("turn updated index is malformed"));
    }
    let turn_id = std::str::from_utf8(&value[18..])
        .map_err(|_| invalid_data("turn updated index identity is not UTF-8"))?
        .to_owned();
    Ok((turn_id, projection_revision))
}

fn decode_tombstone_turn_id(
    key: &EntityKey,
    conversation_prefix_bytes: usize,
) -> io::Result<String> {
    let suffix = key
        .key_bytes()
        .get(conversation_prefix_bytes..)
        .ok_or_else(|| invalid_data("turn tombstone key is malformed"))?;
    if suffix.len() < 9 {
        return Err(invalid_data("turn tombstone key is malformed"));
    }
    std::str::from_utf8(&suffix[8..])
        .map(str::to_owned)
        .map_err(|_| invalid_data("turn tombstone identity is not UTF-8"))
}

fn decode_age_index_identity(key: &EntityKey) -> io::Result<(u64, String, String)> {
    let encoded = key.key_bytes();
    if encoded.len() < 12 {
        return Err(invalid_data("turn tombstone age key is malformed"));
    }
    let deleted_at_ms = u64::from_be_bytes(encoded[..8].try_into().unwrap());
    let mut offset = 8;
    let read_text = |offset: &mut usize| -> io::Result<String> {
        let length_end = offset
            .checked_add(2)
            .filter(|end| *end <= encoded.len())
            .ok_or_else(|| invalid_data("turn tombstone age key is malformed"))?;
        let length = u16::from_be_bytes(encoded[*offset..length_end].try_into().unwrap()) as usize;
        *offset = length_end;
        let value_end = offset
            .checked_add(length)
            .filter(|end| *end <= encoded.len())
            .ok_or_else(|| invalid_data("turn tombstone age key is malformed"))?;
        let value = std::str::from_utf8(&encoded[*offset..value_end])
            .map(str::to_owned)
            .map_err(|_| invalid_data("turn tombstone age identity is not UTF-8"))?;
        *offset = value_end;
        Ok(value)
    };
    let conversation_id = read_text(&mut offset)?;
    let turn_id = read_text(&mut offset)?;
    if offset != encoded.len() || conversation_id.is_empty() || turn_id.is_empty() {
        return Err(invalid_data("turn tombstone age key is malformed"));
    }
    Ok((deleted_at_ms, conversation_id, turn_id))
}

fn after_key(
    key: &EntityKey,
    transaction: &AuthorityTransaction,
    namespace: &str,
) -> io::Result<EntityKey> {
    let mut next = key.key_bytes().to_vec();
    next.push(0);
    entity_key(transaction, namespace, &next)
}

fn decode_u64(value: Option<Vec<u8>>, description: &str) -> io::Result<u64> {
    match value {
        None => Ok(0),
        Some(value) if value.len() == 8 => Ok(u64::from_le_bytes(value.try_into().unwrap())),
        Some(_) => Err(invalid_data(description)),
    }
}

fn decode_turn_value(bytes: &[u8]) -> io::Result<Map<String, Value>> {
    serde_json::from_slice::<Value>(bytes)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("turn document is malformed"))
}

fn projection_head_prefix(
    conversation_id: &str,
    turn_id: &str,
    head_id: &str,
) -> io::Result<Vec<u8>> {
    let mut encoded = conversation_prefix(conversation_id)?;
    push_text_key(&mut encoded, turn_id)?;
    push_text_key(&mut encoded, head_id)?;
    Ok(encoded)
}

fn projection_checkpoint_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = projection_head_prefix(conversation_id, turn_id, head_id)?;
    encoded.push(0);
    entity_key(transaction, TURN_PROJECTION_HEAD_NAMESPACE, &encoded)
}

fn projection_patch_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head_id: &str,
    target_revision: u64,
) -> io::Result<EntityKey> {
    let mut encoded = projection_head_prefix(conversation_id, turn_id, head_id)?;
    encoded.push(1);
    encoded.extend_from_slice(&target_revision.to_be_bytes());
    entity_key(transaction, TURN_PROJECTION_HEAD_NAMESPACE, &encoded)
}

fn projection_head_range(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head_id: &str,
) -> io::Result<(EntityKey, EntityKey)> {
    EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_PROJECTION_HEAD_NAMESPACE,
        &projection_head_prefix(conversation_id, turn_id, head_id)?,
    )
}

fn projection_head_id(attempt_id: &str, base_revision: u64) -> String {
    let mut digest = Sha256::new();
    digest.update(b"tofu.turn-projection-head/v1\0");
    digest.update(attempt_id.as_bytes());
    digest.update([0]);
    digest.update(base_revision.to_be_bytes());
    format!("{:x}", digest.finalize())
}

fn projection_head_from_document(
    document: &Map<String, Value>,
) -> io::Result<Option<ProjectionHeadDescriptor>> {
    let Some(raw) = document.get("_projectionHead") else {
        return Ok(None);
    };
    let raw = raw
        .as_object()
        .ok_or_else(|| invalid_data("Turn projection head is malformed"))?;
    let head_id = raw
        .get("headId")
        .and_then(Value::as_str)
        .filter(|value| value.len() == 64 && value.bytes().all(|byte| byte.is_ascii_hexdigit()))
        .ok_or_else(|| invalid_data("Turn projection head identity is malformed"))?
        .to_owned();
    let attempt_id = raw
        .get("attemptId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty() && value.chars().count() <= 128)
        .ok_or_else(|| invalid_data("Turn projection head attempt is malformed"))?
        .to_owned();
    let base_revision = raw
        .get("baseRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Turn projection head revision is malformed"))?;
    let patch_count = raw
        .get("patchCount")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .filter(|value| *value <= MAX_TURN_PROJECTION_HEAD_PATCHES)
        .ok_or_else(|| invalid_data("Turn projection head count is malformed"))?;
    let patch_bytes = raw
        .get("patchBytes")
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
        .filter(|value| *value <= MAX_TURN_PROJECTION_PATCH_BYTES)
        .ok_or_else(|| invalid_data("Turn projection head bytes are malformed"))?;
    if (patch_count == 0) != (patch_bytes == 0)
        || !matches!(
            document.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        )
        || document.get("currentAttemptId").and_then(Value::as_str) != Some(&attempt_id)
        || document.get("projectionRevision").and_then(Value::as_u64)
            != base_revision.checked_add(patch_count as u64)
        || document
            .get("projection")
            .and_then(Value::as_object)
            .is_none_or(|projection| !projection.is_empty())
    {
        return Err(invalid_data("Turn projection head is inconsistent"));
    }
    Ok(Some(ProjectionHeadDescriptor {
        head_id,
        attempt_id,
        base_revision,
        patch_count,
        patch_bytes,
    }))
}

fn projection_head_value(head: &ProjectionHeadDescriptor) -> Value {
    json!({
        "headId": head.head_id,
        "attemptId": head.attempt_id,
        "baseRevision": head.base_revision,
        "patchCount": head.patch_count,
        "patchBytes": head.patch_bytes,
    })
}

fn store_projection_checkpoint(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head: &ProjectionHeadDescriptor,
    projection: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let value = json!({
        "attemptId": head.attempt_id,
        "revision": head.base_revision,
        "projection": projection,
    });
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: projection_checkpoint_key(transaction, conversation_id, turn_id, &head.head_id)?,
            namespace: PROJECTION_HEAD_DOCUMENT_IDENTITY.to_owned(),
            logical_key: format!("{}\u{1f}checkpoint", head.head_id),
            value_json: serde_json::to_vec(&value)
                .map_err(|_| invalid_data("Turn projection checkpoint cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms,
        },
    )
    .map(|_| ())
}

fn store_projection_head_patch(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head: &ProjectionHeadDescriptor,
    patch: &Map<String, Value>,
    updated_at_ms: u64,
) -> io::Result<()> {
    let target_revision = patch
        .get("targetRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_input("Turn projection patch target is malformed"))?;
    let value = json!({"attemptId": head.attempt_id, "patch": patch});
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: projection_patch_key(
                transaction,
                conversation_id,
                turn_id,
                &head.head_id,
                target_revision,
            )?,
            namespace: PROJECTION_HEAD_DOCUMENT_IDENTITY.to_owned(),
            logical_key: format!("{}\u{1f}{target_revision:020}", head.head_id),
            value_json: serde_json::to_vec(&value)
                .map_err(|_| invalid_data("Turn projection patch cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms,
        },
    )
    .map(|_| ())
}

fn retire_projection_head(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head: Option<&ProjectionHeadDescriptor>,
) -> io::Result<()> {
    if let Some(head) = head {
        let (start, end) =
            projection_head_range(transaction, conversation_id, turn_id, &head.head_id)?;
        database.entity_retire_range(transaction, &start, &end)?;
    }
    Ok(())
}

fn materialize_projection_head(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &mut Map<String, Value>,
    head: &ProjectionHeadDescriptor,
) -> io::Result<()> {
    let conversation_id = document
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn projection head conversation is malformed"))?;
    let turn_id = document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn projection head Turn identity is malformed"))?;
    let checkpoint_key =
        projection_checkpoint_key(transaction, conversation_id, turn_id, &head.head_id)?;
    let checkpoint_stored = database
        .entity_get(transaction, &checkpoint_key)?
        .ok_or_else(|| invalid_data("Turn projection checkpoint is missing"))?;
    let (_, checkpoint_json) = crate::versioned_document::materialize_stored_document(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &checkpoint_stored,
        PROJECTION_HEAD_DOCUMENT_IDENTITY,
    )?;
    let checkpoint = serde_json::from_slice::<Value>(&checkpoint_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("Turn projection checkpoint is malformed"))?;
    if checkpoint.get("attemptId").and_then(Value::as_str) != Some(&head.attempt_id)
        || checkpoint.get("revision").and_then(Value::as_u64) != Some(head.base_revision)
    {
        return Err(invalid_data("Turn projection checkpoint fence differs"));
    }
    let mut projection = checkpoint
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("Turn projection checkpoint value is malformed"))?;
    let mut observed_patch_bytes = 0_usize;
    for offset in 1..=head.patch_count {
        let target_revision = head
            .base_revision
            .checked_add(offset as u64)
            .ok_or_else(|| invalid_data("Turn projection head revision overflows"))?;
        let patch_key = projection_patch_key(
            transaction,
            conversation_id,
            turn_id,
            &head.head_id,
            target_revision,
        )?;
        let patch_stored = database
            .entity_get(transaction, &patch_key)?
            .ok_or_else(|| invalid_data("Turn projection patch is missing"))?;
        let (_, patch_json) = crate::versioned_document::materialize_stored_document(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &patch_stored,
            PROJECTION_HEAD_DOCUMENT_IDENTITY,
        )?;
        let envelope = serde_json::from_slice::<Value>(&patch_json)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("Turn projection patch envelope is malformed"))?;
        if envelope.get("attemptId").and_then(Value::as_str) != Some(&head.attempt_id) {
            return Err(invalid_data("Turn projection patch attempt fence differs"));
        }
        let patch = envelope
            .get("patch")
            .and_then(Value::as_object)
            .ok_or_else(|| invalid_data("Turn projection patch value is malformed"))?;
        if patch.get("baseRevision").and_then(Value::as_u64) != target_revision.checked_sub(1)
            || patch.get("targetRevision").and_then(Value::as_u64) != Some(target_revision)
        {
            return Err(invalid_data("Turn projection patch revision chain differs"));
        }
        observed_patch_bytes = observed_patch_bytes
            .checked_add(
                serde_json::to_vec(patch)
                    .map_err(|_| invalid_data("Turn projection patch cannot be encoded"))?
                    .len(),
            )
            .filter(|bytes| *bytes <= MAX_TURN_PROJECTION_PATCH_BYTES)
            .ok_or_else(|| invalid_data("Turn projection patch chain exceeds its byte bound"))?;
        projection = crate::turn_projection_patch::apply_projection_patch(Some(&projection), patch)
            .map_err(|_| invalid_data("Turn projection patch chain is invalid"))?;
    }
    if observed_patch_bytes != head.patch_bytes {
        return Err(invalid_data("Turn projection patch byte witness differs"));
    }
    document.insert("projection".to_owned(), Value::Object(projection));
    document.remove("_projectionHead");
    Ok(())
}

fn materialize_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    stored: &[u8],
) -> io::Result<Map<String, Value>> {
    let (_, bytes) = crate::versioned_document::materialize_stored_document(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        stored,
        DOCUMENT_IDENTITY,
    )?;
    let mut document = decode_turn_value(&bytes)?;
    if let Some(head) = projection_head_from_document(&document)? {
        materialize_projection_head(database, transaction, &mut document, &head)?;
    }
    Ok(document)
}

fn encode_response(value: &Value) -> io::Result<Vec<u8>> {
    let encoded =
        serde_json::to_vec(value).map_err(|_| invalid_data("turn response cannot be encoded"))?;
    if encoded.len() > MAX_TRANSACTION_IR_LITERAL_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn response exceeds 8 MiB",
        ));
    }
    Ok(encoded)
}

fn normalized_settlement(mut settlement: Map<String, Value>, status: &str) -> Map<String, Value> {
    let mut outcome = settlement
        .get("outcome")
        .and_then(Value::as_str)
        .unwrap_or(if status == "superseded" {
            "interrupted"
        } else {
            status
        })
        .to_owned();
    if !matches!(
        outcome.as_str(),
        "completed" | "interrupted" | "truncated" | "failed"
    ) {
        outcome = "failed".to_owned();
    }
    let cause = settlement
        .get("cause")
        .and_then(Value::as_str)
        .unwrap_or("ingested")
        .to_owned();
    settlement.insert("outcome".to_owned(), Value::String(outcome.clone()));
    settlement.insert("cause".to_owned(), Value::String(cause.clone()));
    settlement
        .entry("providerFinishReason")
        .or_insert(Value::Null);
    settlement.entry("error").or_insert(Value::Null);
    settlement
        .entry("resumeOptions")
        .or_insert_with(|| json!([]));
    settlement.entry("streamState").or_insert(Value::Null);
    settlement.entry("evidence").or_insert_with(|| {
        Value::String(
            match (outcome.as_str(), cause.as_str()) {
                (_, "server_restart" | "conversation_deleted") => "system_recovery",
                ("truncated", _) => "provider_limit",
                ("failed", _) => "generation_error",
                ("completed", "provider_finished") => "legacy_finish",
                _ => "external_authority",
            }
            .to_owned(),
        )
    });
    settlement
}

fn terminalize_restored_projection(value: &mut Value) {
    const RUNTIME_KEYS: &[&str] = &[
        "_commandPending",
        "_flowRunId",
        "_needsStart",
        "_runId",
        "_streamCursor",
        "_streaming",
        "activeTaskId",
        "approvalRequired",
        "attemptId",
        "isStreaming",
        "runId",
    ];
    match value {
        Value::Array(values) => {
            for value in values {
                terminalize_restored_projection(value);
            }
        }
        Value::Object(object) => {
            for key in RUNTIME_KEYS {
                object.remove(*key);
            }
            if let Some(Value::Array(rounds)) = object.get_mut("toolRounds") {
                for round in rounds {
                    if let Some(round) = round.as_object_mut() {
                        let terminal = matches!(
                            round.get("status").and_then(Value::as_str),
                            Some(
                                "abort"
                                    | "aborted"
                                    | "completed"
                                    | "done"
                                    | "error"
                                    | "failed"
                                    | "not-run"
                                    | "not_run"
                                    | "rejected"
                                    | "skipped"
                                    | "succeeded"
                                    | "success"
                                    | "unknown"
                            )
                        );
                        if !terminal {
                            round.insert("status".to_owned(), Value::String("aborted".to_owned()));
                        }
                    }
                }
            }
            if let Some(Value::Object(trace)) = object.get_mut("timingTrace") {
                trace.insert("status".to_owned(), Value::String("aborted".to_owned()));
                trace.insert("running".to_owned(), Value::Bool(false));
            }
            if object.get("type").and_then(Value::as_str) == Some("tool_use") {
                if let Some(Value::Object(result)) = object.get_mut("result") {
                    let terminal = matches!(
                        result.get("status").and_then(Value::as_str),
                        Some(
                            "abort"
                                | "aborted"
                                | "completed"
                                | "done"
                                | "error"
                                | "failed"
                                | "not-run"
                                | "not_run"
                                | "rejected"
                                | "skipped"
                                | "succeeded"
                                | "success"
                                | "unknown"
                        )
                    );
                    if !terminal {
                        result.insert("status".to_owned(), Value::String("aborted".to_owned()));
                    }
                }
            }
            for value in object.values_mut() {
                terminalize_restored_projection(value);
            }
        }
        _ => {}
    }
}

fn public_turn(
    mut document: Map<String, Value>,
    current_execution_epoch: u64,
) -> io::Result<Value> {
    let document_epoch = match document.remove("_executionEpoch") {
        None => 0,
        Some(Value::Number(value)) => value
            .as_u64()
            .ok_or_else(|| invalid_data("turn execution epoch is malformed"))?,
        Some(_) => return Err(invalid_data("turn execution epoch is malformed")),
    };
    if document_epoch != current_execution_epoch {
        document.insert("currentAttemptId".to_owned(), Value::Null);
        document.insert("runId".to_owned(), Value::String(String::new()));
        if matches!(
            document.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        ) {
            document.insert("status".to_owned(), Value::String("interrupted".to_owned()));
            document.insert(
                "settlement".to_owned(),
                json!({
                    "outcome": "interrupted",
                    "cause": "conversation_deleted",
                    "resumeOptions": []
                }),
            );
        }
        if let Some(projection) = document.get_mut("projection") {
            terminalize_restored_projection(projection);
        }
    }
    let status = document
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn status is malformed"))?
        .to_owned();
    let settlement = document
        .remove("settlement")
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("turn settlement is malformed"))?;
    let settlement = if matches!(status.as_str(), "pending" | "running") {
        settlement
    } else {
        normalized_settlement(settlement, &status)
    };
    let outcome = settlement
        .get("outcome")
        .and_then(Value::as_str)
        .unwrap_or(&status);
    if matches!(
        status.as_str(),
        "completed" | "interrupted" | "truncated" | "failed" | "superseded"
    ) && matches!(
        outcome,
        "completed" | "interrupted" | "truncated" | "failed"
    ) {
        document.insert("status".to_owned(), Value::String(outcome.to_owned()));
    }
    document.insert("settlement".to_owned(), Value::Object(settlement));
    Ok(Value::Object(document))
}

fn conversation_sync_event_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    sync_sequence: u64,
) -> io::Result<EntityKey> {
    let mut encoded = conversation_prefix(conversation_id)?;
    encoded.extend_from_slice(&sync_sequence.to_be_bytes());
    entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded)
}

fn conversation_sync_age_index_key(
    transaction: &AuthorityTransaction,
    occurred_at_ms: u64,
    conversation_id: &str,
    sync_sequence: u64,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(18 + conversation_id.len());
    encoded.extend_from_slice(&occurred_at_ms.to_be_bytes());
    push_text_key(&mut encoded, conversation_id)?;
    encoded.extend_from_slice(&sync_sequence.to_be_bytes());
    entity_key(transaction, CONVERSATION_SYNC_AGE_INDEX_NAMESPACE, &encoded)
}

fn attempt_sync_reference_prefix(attempt_id: &str) -> io::Result<Vec<u8>> {
    let mut encoded = Vec::with_capacity(2 + attempt_id.len());
    push_text_key(&mut encoded, attempt_id)?;
    Ok(encoded)
}

fn attempt_sync_reference_key(
    transaction: &AuthorityTransaction,
    attempt_id: &str,
    conversation_id: &str,
    sync_sequence: u64,
) -> io::Result<EntityKey> {
    let mut encoded = attempt_sync_reference_prefix(attempt_id)?;
    push_text_key(&mut encoded, conversation_id)?;
    encoded.extend_from_slice(&sync_sequence.to_be_bytes());
    entity_key(
        transaction,
        ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE,
        &encoded,
    )
}

fn sync_age_index_value(event: &Value) -> io::Result<Vec<u8>> {
    let text = |field: &str| {
        event
            .get(field)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("conversation sync retention identity is malformed"))
    };
    let sequence = event
        .get("syncSeq")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation sync retention sequence is malformed"))?;
    let occurred_at_ms = event
        .get("occurredAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation sync retention timestamp is malformed"))?;
    let mut locator = Map::from_iter([
        (
            "conversationId".to_owned(),
            Value::String(text("conversationId")?.to_owned()),
        ),
        ("syncSeq".to_owned(), Value::from(sequence)),
        ("occurredAt".to_owned(), Value::from(occurred_at_ms)),
    ]);
    if event.get("type").and_then(Value::as_str) == Some("attempt.event") {
        let attempt_id = event
            .get("attemptId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("attempt sync reference identity is malformed"))?;
        let attempt_sequence = event
            .get("payload")
            .and_then(|payload| payload.get("event"))
            .and_then(|attempt_event| attempt_event.get("seq"))
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("attempt sync reference sequence is malformed"))?;
        locator.insert("attemptId".to_owned(), Value::String(attempt_id.to_owned()));
        locator.insert("attemptSequence".to_owned(), Value::from(attempt_sequence));
    }
    serde_json::to_vec(&Value::Object(locator))
        .map_err(|_| invalid_data("conversation sync retention index cannot be encoded"))
}

fn store_sync_retention_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    event: &Value,
) -> io::Result<()> {
    let conversation_id = event
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("conversation sync identity is malformed"))?;
    let sync_sequence = event
        .get("syncSeq")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation sync sequence is malformed"))?;
    let occurred_at_ms = event
        .get("occurredAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("conversation sync timestamp is malformed"))?;
    database.entity_put(
        transaction,
        conversation_sync_age_index_key(
            transaction,
            occurred_at_ms,
            conversation_id,
            sync_sequence,
        )?,
        sync_age_index_value(event)?,
    )?;
    if event.get("type").and_then(Value::as_str) == Some("attempt.event") {
        let attempt_id = event
            .get("attemptId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("attempt sync reference identity is malformed"))?;
        let attempt_sequence = event
            .get("payload")
            .and_then(|payload| payload.get("event"))
            .and_then(|attempt_event| attempt_event.get("seq"))
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("attempt sync reference sequence is malformed"))?;
        database.entity_put(
            transaction,
            attempt_sync_reference_key(transaction, attempt_id, conversation_id, sync_sequence)?,
            attempt_sequence.to_le_bytes().to_vec(),
        )?;
    }
    Ok(())
}

fn conversation_sync_event(
    conversation_id: &str,
    sync_sequence: u64,
    change_type: &str,
    occurred_at_ms: u64,
    payload: Value,
    turn_id: Option<&str>,
    attempt_id: Option<&str>,
) -> Value {
    let mut event = json!({
        "contract": "tofu.conversation-sync.event/v1",
        "type": change_type,
        "conversationId": conversation_id,
        "syncSeq": sync_sequence,
        "occurredAt": occurred_at_ms,
        "payload": payload,
    });
    if let Some(turn_id) = turn_id.filter(|identity| !identity.is_empty()) {
        event["turnId"] = Value::String(turn_id.to_owned());
    }
    if let Some(attempt_id) = attempt_id.filter(|identity| !identity.is_empty()) {
        event["attemptId"] = Value::String(attempt_id.to_owned());
    }
    event
}

fn store_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AppendSettledRequest,
    turn: &Value,
    attempt: Option<&Value>,
    revision: u64,
) -> io::Result<()> {
    let turns = vec![turn.clone()];
    let attempts = attempt.iter().map(|value| (*value).clone()).collect();
    store_turn_upsert_sync_event(
        database,
        transaction,
        &request.conversation_id,
        turns,
        attempts,
        Vec::new(),
        Vec::new(),
        revision,
        request.created_at_ms,
        request.committed_at_ms,
        Some(&request.turn_id),
        request.attempt_id.as_deref(),
    )
}

#[allow(clippy::too_many_arguments)]
fn store_turn_upsert_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turns: Vec<Value>,
    attempts: Vec<Value>,
    queue_item_upserts: Vec<Value>,
    removed_queue_ids: Vec<String>,
    revision: u64,
    occurred_at_ms: u64,
    committed_at_ms: u64,
    turn_id: Option<&str>,
    attempt_id: Option<&str>,
) -> io::Result<()> {
    let head_key = entity_key(
        transaction,
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )?;
    let sequence = decode_u64(
        database.entity_get(transaction, &head_key)?,
        "conversation sync head is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("conversation sync sequence overflow"))?;
    let mut payload = json!({
        "turns": turns,
        "attempts": attempts,
        "conversationRevision": revision
    });
    if !queue_item_upserts.is_empty() {
        payload["queueItemUpserts"] = Value::Array(queue_item_upserts);
    }
    if !removed_queue_ids.is_empty() {
        payload["removedQueueIds"] = serde_json::to_value(removed_queue_ids)
            .map_err(|_| invalid_data("removed queue identities cannot be encoded"))?;
    }
    let event = conversation_sync_event(
        conversation_id,
        sequence,
        "turn.upsert",
        occurred_at_ms,
        payload,
        turn_id,
        attempt_id,
    );
    let logical_key = format!("{conversation_id}\u{1f}{sequence:020}");
    let mut encoded_key = conversation_prefix(conversation_id)?;
    encoded_key.extend_from_slice(&sequence.to_be_bytes());
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded_key)?,
            namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
            logical_key,
            value_json: serde_json::to_vec(&event)
                .map_err(|_| invalid_data("sync event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: committed_at_ms,
        },
    )?;
    store_sync_retention_indexes(database, transaction, &event)?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())
}

fn store_delete_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    deleted_turn_ids: &[String],
    removed_queue_ids: &[String],
    revision: u64,
    occurred_at_ms: u64,
) -> io::Result<()> {
    let head_key = entity_key(
        transaction,
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )?;
    let sequence = decode_u64(
        database.entity_get(transaction, &head_key)?,
        "conversation sync head is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("conversation sync sequence overflow"))?;
    let mut payload = json!({
        "deletedTurnIds": deleted_turn_ids,
        "conversationRevision": revision
    });
    if !removed_queue_ids.is_empty() {
        payload["removedQueueIds"] = serde_json::to_value(removed_queue_ids)
            .map_err(|_| invalid_data("removed queue identities cannot be encoded"))?;
    }
    let event = conversation_sync_event(
        conversation_id,
        sequence,
        "turn.deleted",
        occurred_at_ms,
        payload,
        None,
        None,
    );
    let logical_key = format!("{conversation_id}\u{1f}{sequence:020}");
    let mut encoded_key = conversation_prefix(conversation_id)?;
    encoded_key.extend_from_slice(&sequence.to_be_bytes());
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded_key)?,
            namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
            logical_key,
            value_json: serde_json::to_vec(&event)
                .map_err(|_| invalid_data("sync delete event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: occurred_at_ms,
        },
    )?;
    store_sync_retention_indexes(database, transaction, &event)?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())
}

fn projection_patch(
    before: &Map<String, Value>,
    after: &Map<String, Value>,
    base: u64,
) -> io::Result<Value> {
    let target = base
        .checked_add(1)
        .ok_or_else(|| invalid_data("Turn projection revision overflows"))?;
    crate::turn_projection_patch::build_projection_patch(before, after, base, target)
        .map(Value::Object)
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::OutOfMemory,
                "Turn projection patch exceeds its deterministic resource bound",
            )
        })
}

struct AttemptEventAppend<'a> {
    conversation_id: &'a str,
    turn_id: &'a str,
    attempt_id: &'a str,
    projection_revision: u64,
    event_type: &'a str,
    payload: Value,
    occurred_at_ms: u64,
    publish_conversation_sync: bool,
}

fn append_attempt_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    append: AttemptEventAppend<'_>,
) -> io::Result<Value> {
    let AttemptEventAppend {
        conversation_id,
        turn_id,
        attempt_id,
        projection_revision,
        event_type,
        payload,
        occurred_at_ms,
        publish_conversation_sync,
    } = append;
    let head_key = attempt_event_head_key(transaction, attempt_id)?;
    let sequence = decode_u64(
        database.entity_get(transaction, &head_key)?,
        "attempt event head is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("attempt event sequence overflow"))?;
    let event = json!({
        "conversationId": conversation_id,
        "turnId": turn_id,
        "attemptId": attempt_id,
        "seq": sequence,
        "projectionRevision": projection_revision,
        "type": event_type,
        "payload": payload
    });
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: attempt_event_key(transaction, attempt_id, sequence)?,
            namespace: ATTEMPT_EVENT_DOCUMENT_IDENTITY.to_owned(),
            logical_key: format!("{attempt_id}\u{1f}{sequence:020}"),
            value_json: serde_json::to_vec(&event)
                .map_err(|_| invalid_data("attempt event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: occurred_at_ms,
        },
    )?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())?;
    if publish_conversation_sync {
        let sync_head_key = entity_key(
            transaction,
            CONVERSATION_SYNC_HEAD_NAMESPACE,
            &conversation_prefix(conversation_id)?,
        )?;
        let sync_sequence = decode_u64(
            database.entity_get(transaction, &sync_head_key)?,
            "conversation sync head is malformed",
        )?
        .checked_add(1)
        .ok_or_else(|| invalid_data("conversation sync sequence overflow"))?;
        let envelope = conversation_sync_event(
            conversation_id,
            sync_sequence,
            "attempt.event",
            occurred_at_ms,
            json!({"event": event}),
            Some(turn_id),
            Some(attempt_id),
        );
        let logical_key = format!("{conversation_id}\u{1f}{sync_sequence:020}");
        let mut encoded_key = conversation_prefix(conversation_id)?;
        encoded_key.extend_from_slice(&sync_sequence.to_be_bytes());
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded_key)?,
                namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
                logical_key,
                value_json: serde_json::to_vec(&envelope)
                    .map_err(|_| invalid_data("attempt sync event cannot be encoded"))?,
                expected_version: Some(0),
                updated_at_ms: occurred_at_ms,
            },
        )?;
        store_sync_retention_indexes(database, transaction, &envelope)?;
        database.entity_put(
            transaction,
            sync_head_key,
            sync_sequence.to_le_bytes().to_vec(),
        )?;
    }
    Ok(event)
}

fn store_projection_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ProjectionUpdateRequest,
    before: &Map<String, Value>,
    after: &Map<String, Value>,
    conversation_revision: u64,
    deleted_turn_ids: &[String],
) -> io::Result<()> {
    let conversation_id = &request.conversation_id;
    let base_projection_revision = request.expected_projection_revision;
    let head_key = entity_key(
        transaction,
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )?;
    let sequence = decode_u64(
        database.entity_get(transaction, &head_key)?,
        "conversation sync head is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("conversation sync sequence overflow"))?;
    let turn_patch = json!({
        "turnId": request.turn_id,
        "baseProjectionRevision": base_projection_revision,
        "targetProjectionRevision": base_projection_revision + 1,
        "updatedAt": request.updated_at_ms,
        "projectionPatch": projection_patch(before, after, base_projection_revision)?
    });
    let mut payload = Map::from_iter([
        ("turnPatches".to_owned(), Value::Array(vec![turn_patch])),
        (
            "conversationRevision".to_owned(),
            Value::from(conversation_revision),
        ),
    ]);
    if !deleted_turn_ids.is_empty() {
        payload.insert(
            "deletedTurnIds".to_owned(),
            serde_json::to_value(deleted_turn_ids)
                .map_err(|_| invalid_data("deleted Turn identities cannot be encoded"))?,
        );
    }
    let event = conversation_sync_event(
        conversation_id,
        sequence,
        "turn.patch",
        request.updated_at_ms,
        Value::Object(payload),
        Some(&request.turn_id),
        None,
    );
    let logical_key = format!("{conversation_id}\u{1f}{sequence:020}");
    let mut encoded_key = conversation_prefix(conversation_id)?;
    encoded_key.extend_from_slice(&sequence.to_be_bytes());
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded_key)?,
            namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
            logical_key,
            value_json: serde_json::to_vec(&event)
                .map_err(|_| invalid_data("sync projection event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: request.updated_at_ms,
        },
    )?;
    store_sync_retention_indexes(database, transaction, &event)?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())
}

pub(crate) fn stable_clone_identity(seed: &[u8; 32], domain: &[u8], source: &str) -> Uuid {
    let mut hasher = blake3::Hasher::new_keyed(seed);
    hasher.update(b"tofu-db.conversation-clone.v1\0");
    hasher.update(domain);
    hasher.update(&[0]);
    hasher.update(source.as_bytes());
    let digest = hasher.finalize();
    let mut bytes: [u8; 16] = digest.as_bytes()[..16].try_into().unwrap();
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Uuid::from_bytes(bytes)
}

fn clone_projection_value(
    value: &Value,
    identity_seed: &[u8; 32],
    turn_ids: &BTreeMap<String, String>,
) -> Value {
    const RUNTIME_KEYS: &[&str] = &[
        "_activeAttemptId",
        "_attemptId",
        "_authoritativeActiveTaskIds",
        "_commandPending",
        "_flowRunId",
        "_msgId",
        "_needsStart",
        "_runId",
        "_streamCursor",
        "_streaming",
        "activeTaskId",
        "approvalRequired",
        "attemptId",
        "isStreaming",
        "runId",
    ];
    const TASK_ID_KEYS: &[&str] = &["_proactiveTaskId", "_taskId", "_translateTaskId", "taskId"];
    match value {
        Value::Array(values) => Value::Array(
            values
                .iter()
                .map(|value| clone_projection_value(value, identity_seed, turn_ids))
                .collect(),
        ),
        Value::Object(source) => {
            let mut cloned = Map::new();
            for (key, value) in source {
                if RUNTIME_KEYS.contains(&key.as_str()) {
                    continue;
                }
                if matches!(key.as_str(), "_turnId" | "turnId") {
                    if let Some(source_id) = value.as_str() {
                        if let Some(destination_id) = turn_ids.get(source_id) {
                            cloned.insert(key.clone(), Value::String(destination_id.clone()));
                        }
                    }
                    continue;
                }
                if TASK_ID_KEYS.contains(&key.as_str()) {
                    if let Some(source_id) = value.as_str().filter(|value| !value.is_empty()) {
                        cloned.insert(
                            key.clone(),
                            Value::String(format!(
                                "clone-task-{}",
                                stable_clone_identity(identity_seed, b"task", source_id)
                            )),
                        );
                    }
                    continue;
                }
                if matches!(key.as_str(), "archiveId" | "_compactionArchiveId") {
                    if let Some(source_id) = value.as_str().filter(|value| !value.is_empty()) {
                        cloned.insert(
                            key.clone(),
                            Value::String(format!(
                                "clone-archive-{}",
                                stable_clone_identity(identity_seed, b"archive", source_id)
                            )),
                        );
                    }
                    continue;
                }
                cloned.insert(
                    key.clone(),
                    clone_projection_value(value, identity_seed, turn_ids),
                );
            }
            Value::Object(cloned)
        }
        _ => value.clone(),
    }
}

pub(crate) fn clone_conversation_turns(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    source_conversation_id: &str,
    destination_conversation_id: &str,
    identity_seed: [u8; 32],
    cloned_at_ms: u64,
) -> io::Result<CloneTurnSummary> {
    let prefix = conversation_prefix(source_conversation_id)?;
    let rows = scan_conversation_identity_namespace(
        database,
        transaction,
        TURN_DOCUMENT_NAMESPACE,
        &prefix,
        MAX_CLONE_ROWS,
    )?;
    let source_epoch =
        crate::conversation_header::execution_epoch(database, transaction, source_conversation_id)?;
    let mut source_turns = Vec::with_capacity(rows.len());
    let mut materialized_bytes = 0_usize;
    for (_, stored) in rows {
        let turn = public_turn(
            materialize_turn(database, transaction, &stored)?,
            source_epoch,
        )?;
        materialized_bytes = materialized_bytes
            .checked_add(
                serde_json::to_vec(&turn)
                    .map_err(|_| invalid_data("source Turn cannot be encoded"))?
                    .len(),
            )
            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "conversation clone exceeds 8 MiB of Turn projection",
                )
            })?;
        source_turns.push(turn);
    }
    let mut turn_ids = BTreeMap::new();
    for turn in &source_turns {
        let source_turn_id = turn
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("source Turn identity is malformed"))?;
        let destination_turn_id =
            stable_clone_identity(&identity_seed, b"turn", source_turn_id).to_string();
        if turn_ids
            .insert(source_turn_id.to_owned(), destination_turn_id)
            .is_some()
        {
            return Err(invalid_data("source Turn identity is duplicated"));
        }
    }
    for destination_turn_id in turn_ids.values() {
        let claim_key =
            global_identity_claim_key(transaction, TURN_ID_CLAIM_NAMESPACE, destination_turn_id)?;
        if database.entity_get(transaction, &claim_key)?.is_some() {
            return Err(conflict("cloned Turn identity already exists"));
        }
    }

    let mut lane_counts = BTreeMap::<String, u64>::new();
    let mut lane_heads = BTreeMap::<String, u64>::new();
    let mut lane_ordinals = BTreeSet::<(String, u64)>::new();
    let mut cloned_projection_bytes = 0_usize;
    let claim_owner = transaction.owner_user_id().to_be_bytes().to_vec();
    for source_turn in source_turns {
        let source = source_turn
            .as_object()
            .ok_or_else(|| invalid_data("source Turn is malformed"))?;
        let source_turn_id = source
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("source Turn identity is malformed"))?;
        let destination_turn_id = turn_ids
            .get(source_turn_id)
            .ok_or_else(|| invalid_data("cloned Turn identity is missing"))?
            .clone();
        let lane_id = source
            .get("laneId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("source Turn lane is malformed"))?
            .to_owned();
        let ordinal = source
            .get("ordinal")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("source Turn ordinal is malformed"))?;
        if !lane_ordinals.insert((lane_id.clone(), ordinal)) {
            return Err(invalid_data("source Turn lane ordinal is duplicated"));
        }
        let actor = source
            .get("actor")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("source Turn actor is malformed"))?;
        let kind = source
            .get("kind")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("source Turn kind is malformed"))?;
        let source_status = source
            .get("status")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("source Turn status is malformed"))?;
        let is_live = matches!(source_status, "pending" | "running");
        let status = if is_live {
            "interrupted"
        } else {
            source_status
        };
        let created_at_ms = source
            .get("createdAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("source Turn creation timestamp is malformed"))?;
        let source_updated_at_ms = source
            .get("updatedAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("source Turn update timestamp is malformed"))?;
        let updated_at_ms = if is_live {
            cloned_at_ms
        } else {
            source_updated_at_ms
        };
        let parent_turn_id = match source.get("parentTurnId") {
            None | Some(Value::Null) => Value::Null,
            Some(Value::String(parent)) => Value::String(
                turn_ids
                    .get(parent)
                    .ok_or_else(|| invalid_data("source Turn parent is missing"))?
                    .clone(),
            ),
            Some(_) => return Err(invalid_data("source Turn parent is malformed")),
        };
        let mut projection = clone_projection_value(
            source
                .get("projection")
                .ok_or_else(|| invalid_data("source Turn projection is missing"))?,
            &identity_seed,
            &turn_ids,
        );
        if is_live {
            terminalize_restored_projection(&mut projection);
        }
        cloned_projection_bytes = cloned_projection_bytes
            .checked_add(
                serde_json::to_vec(&projection)
                    .map_err(|_| invalid_data("cloned Turn projection cannot be encoded"))?
                    .len(),
            )
            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "conversation clone exceeds 8 MiB of cloned projection",
                )
            })?;
        let settlement = if is_live {
            json!({
                "outcome": "interrupted",
                "cause": "conversation_cloned_snapshot",
                "resumeOptions": []
            })
        } else {
            source
                .get("settlement")
                .cloned()
                .ok_or_else(|| invalid_data("source Turn settlement is missing"))?
        };
        let activity_timestamp =
            effective_activity_timestamp(projection.get("timestamp"), created_at_ms);
        let document = json!({
            "turnId": destination_turn_id,
            "presentationId": destination_turn_id,
            "conversationId": destination_conversation_id,
            "laneId": lane_id,
            "parentTurnId": parent_turn_id,
            "ordinal": ordinal,
            "actor": actor,
            "kind": kind,
            "runId": "",
            "status": status,
            "currentAttemptId": null,
            "projection": projection,
            "projectionRevision": 1,
            "settlement": settlement,
            "createdAt": created_at_ms,
            "updatedAt": updated_at_ms,
            "_executionEpoch": 0
        });
        let document_key = turn_key(
            transaction,
            destination_conversation_id,
            &destination_turn_id,
        )?;
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: document_key.clone(),
                namespace: DOCUMENT_IDENTITY.to_owned(),
                logical_key: turn_logical_key(destination_conversation_id, &destination_turn_id),
                value_json: serde_json::to_vec(&document)
                    .map_err(|_| invalid_data("cloned Turn cannot be encoded"))?,
                expected_version: Some(0),
                updated_at_ms: cloned_at_ms,
            },
        )?;
        let stored = database
            .entity_get(transaction, &document_key)?
            .ok_or_else(|| invalid_data("staged cloned Turn disappeared"))?;
        database.entity_put(
            transaction,
            global_identity_claim_key(transaction, TURN_ID_CLAIM_NAMESPACE, &destination_turn_id)?,
            claim_owner.clone(),
        )?;
        database.entity_put(
            transaction,
            lane_index_key(transaction, destination_conversation_id, &lane_id, ordinal)?,
            stored,
        )?;
        put_lane_compaction_index(
            database,
            transaction,
            document
                .as_object()
                .ok_or_else(|| invalid_data("cloned Turn cannot be indexed"))?,
        )?;
        if lane_id == "main" {
            database.entity_put(
                transaction,
                activity_index_key(transaction, destination_conversation_id, ordinal)?,
                encode_activity_index_value(activity_timestamp),
            )?;
        }
        database.entity_put(
            transaction,
            updated_index_key(
                transaction,
                destination_conversation_id,
                updated_at_ms,
                &destination_turn_id,
            )?,
            encode_updated_index_value(&destination_turn_id, 1)?,
        )?;
        *lane_counts.entry(lane_id.clone()).or_default() += 1;
        lane_heads
            .entry(lane_id)
            .and_modify(|head| *head = (*head).max(ordinal))
            .or_insert(ordinal);
    }
    for (lane_id, count) in &lane_counts {
        database.entity_put(
            transaction,
            lane_count_key(transaction, destination_conversation_id, lane_id)?,
            count.to_le_bytes().to_vec(),
        )?;
        database.entity_put(
            transaction,
            entity_key(
                transaction,
                TURN_LANE_HEAD_NAMESPACE,
                &lane_prefix(destination_conversation_id, lane_id)?,
            )?,
            lane_heads[lane_id].to_le_bytes().to_vec(),
        )?;
    }
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        destination_conversation_id,
    )?;
    Ok(CloneTurnSummary {
        turn_count: turn_ids.len(),
        main_count: lane_counts.get("main").copied().unwrap_or(0),
    })
}

pub(crate) fn append_settled(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AppendSettledRequest,
) -> io::Result<Vec<u8>> {
    let turn_claim_key =
        global_identity_claim_key(transaction, TURN_ID_CLAIM_NAMESPACE, &request.turn_id)?;
    if database.entity_get(transaction, &turn_claim_key)?.is_some() {
        return Err(conflict("turn identity already exists"));
    }
    let attempt_claim_key = request
        .attempt_id
        .as_deref()
        .map(|attempt_id| {
            global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)
        })
        .transpose()?;
    if let Some(claim_key) = &attempt_claim_key {
        if database.entity_get(transaction, claim_key)?.is_some() {
            return Err(conflict("attempt identity already exists"));
        }
    }
    let claim_owner = transaction.owner_user_id().to_be_bytes().to_vec();
    crate::conversation_header::ensure_for_turn(
        database,
        transaction,
        &request.conversation_id,
        &request.defaults,
        request.created_at_ms,
        request.committed_at_ms,
    )?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let head_key = entity_key(
        transaction,
        TURN_LANE_HEAD_NAMESPACE,
        &lane_prefix(&request.conversation_id, &request.lane_id)?,
    )?;
    let ordinal = match database.entity_get(transaction, &head_key)? {
        None => 0,
        Some(value) => decode_u64(Some(value), "turn lane head is malformed")?
            .checked_add(1)
            .ok_or_else(|| invalid_data("turn lane ordinal overflow"))?,
    };
    let count_key = lane_count_key(transaction, &request.conversation_id, &request.lane_id)?;
    let lane_count = decode_u64(
        database.entity_get(transaction, &count_key)?,
        "turn lane count is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("turn lane count overflow"))?;
    let parent_turn_id = if ordinal == 0 {
        Value::Null
    } else {
        let prefix = lane_prefix(&request.conversation_id, &request.lane_id)?;
        let (start, _) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            TURN_LANE_INDEX_NAMESPACE,
            &prefix,
        )?;
        let end = lane_index_key(
            transaction,
            &request.conversation_id,
            &request.lane_id,
            ordinal,
        )?;
        match database
            .entity_scan_reverse(transaction, &start, &end, 1)?
            .pop()
        {
            None => Value::Null,
            Some((_, previous)) => {
                let previous = materialize_turn(database, transaction, &previous)?;
                Value::String(
                    previous
                        .get("turnId")
                        .and_then(Value::as_str)
                        .ok_or_else(|| invalid_data("turn predecessor identity is malformed"))?
                        .to_owned(),
                )
            }
        }
    };
    let projection = serde_json::from_slice::<Value>(&request.projection_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("turn projection must be an object"))?;
    let settlement = serde_json::from_slice::<Value>(&request.settlement_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("turn settlement must be an object"))?;
    let activity_timestamp =
        effective_activity_timestamp(projection.get("timestamp"), request.created_at_ms);
    let document = json!({
        "turnId": request.turn_id,
        "presentationId": request.turn_id,
        "conversationId": request.conversation_id,
        "laneId": request.lane_id,
        "parentTurnId": parent_turn_id,
        "ordinal": ordinal,
        "actor": request.actor,
        "kind": request.kind,
        "runId": request.run_id,
        "status": request.status,
        "currentAttemptId": request.attempt_id,
        "projection": projection,
        "projectionRevision": 1,
        "settlement": settlement,
        "createdAt": request.created_at_ms,
        "updatedAt": request.created_at_ms,
        "_executionEpoch": execution_epoch
    });
    let document_json =
        serde_json::to_vec(&document).map_err(|_| invalid_data("turn cannot be encoded"))?;
    let document_key = turn_key(transaction, &request.conversation_id, &request.turn_id)?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: document_key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&request.conversation_id, &request.turn_id),
            value_json: document_json,
            expected_version: Some(0),
            updated_at_ms: request.committed_at_ms,
        },
    )?;
    database.entity_put(transaction, turn_claim_key, claim_owner.clone())?;
    let stored = database
        .entity_get(transaction, &document_key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &request.conversation_id,
            request.created_at_ms,
            &request.turn_id,
        )?,
        encode_updated_index_value(&request.turn_id, 1)?,
    )?;
    database.entity_put(
        transaction,
        lane_index_key(
            transaction,
            &request.conversation_id,
            &request.lane_id,
            ordinal,
        )?,
        stored,
    )?;
    put_lane_compaction_index(
        database,
        transaction,
        document
            .as_object()
            .ok_or_else(|| invalid_data("Turn cannot be indexed"))?,
    )?;
    if request.lane_id == "main" {
        database.entity_put(
            transaction,
            activity_index_key(transaction, &request.conversation_id, ordinal)?,
            encode_activity_index_value(activity_timestamp),
        )?;
    }
    database.entity_put(transaction, head_key, ordinal.to_le_bytes().to_vec())?;
    database.entity_put(transaction, count_key, lane_count.to_le_bytes().to_vec())?;
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    let revision = crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &request.conversation_id,
        request.created_at_ms,
        request.committed_at_ms,
        request.lane_id == "main",
    )?;
    let turn = public_turn(
        decode_turn_value(serde_json::to_vec(&document).unwrap().as_slice())?,
        execution_epoch,
    )?;
    let attempt = request.attempt_id.as_ref().map(|attempt_id| {
        json!({
            "attemptId": attempt_id,
            "conversationId": request.conversation_id,
            "turnId": request.turn_id,
            "commandId": request.command_id,
            "taskId": "",
            "operation": "ingest",
            "status": request.status,
            "baseProjectionRevision": 0,
            "resumeAnchor": {},
            "createdAt": request.created_at_ms,
            "startedAt": request.created_at_ms,
            "settledAt": request.created_at_ms
        })
    });
    if let (Some(attempt_id), Some(attempt)) = (&request.attempt_id, &attempt) {
        let command_key =
            attempt_command_key(transaction, &request.conversation_id, &request.command_id)?;
        if database.entity_get(transaction, &command_key)?.is_some() {
            return Err(conflict("attempt command identity already exists"));
        }
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: attempt_key(transaction, &request.conversation_id, attempt_id)?,
                namespace: "generation_attempts".to_owned(),
                logical_key: attempt_id.clone(),
                value_json: serde_json::to_vec(attempt)
                    .map_err(|_| invalid_data("attempt cannot be encoded"))?,
                expected_version: Some(0),
                updated_at_ms: request.committed_at_ms,
            },
        )?;
        if let Some(attempt_document) = attempt.as_object() {
            if let Some(settled_at_ms) = terminal_attempt_settled_at(attempt_document)? {
                database.entity_put(
                    transaction,
                    attempt_event_retention_index_key(transaction, settled_at_ms, attempt_id)?,
                    attempt_event_retention_index_value(attempt_document, 1)?,
                )?;
            }
        }
        append_attempt_turn_directory(
            database,
            transaction,
            &request.conversation_id,
            &request.turn_id,
            None,
            attempt
                .as_object()
                .ok_or_else(|| invalid_data("attempt cannot be indexed"))?,
        )?;
        database.entity_put(
            transaction,
            attempt_claim_key
                .clone()
                .ok_or_else(|| invalid_data("attempt claim key is missing"))?,
            encode_attempt_locator(transaction.owner_user_id(), &request.conversation_id)?,
        )?;
        let attempt_document = attempt
            .as_object()
            .ok_or_else(|| invalid_data("attempt cannot be indexed"))?;
        if attempt_is_live(attempt_document) {
            database.entity_put(
                transaction,
                recovery_index_key(transaction, request.created_at_ms, attempt_id)?,
                recovery_index_value(
                    attempt_document,
                    serde_json::to_vec(&projection)
                        .map_err(|_| invalid_data("turn projection cannot be encoded"))?
                        .len(),
                )?,
            )?;
            set_lane_live_attempt(
                database,
                transaction,
                &request.conversation_id,
                &request.lane_id,
                &request.turn_id,
                attempt_id,
                true,
            )?;
        }
        database.entity_put(transaction, command_key, attempt_id.as_bytes().to_vec())?;
        database.entity_put(
            transaction,
            attempt_turn_count_key(transaction, &request.conversation_id, &request.turn_id)?,
            1_u64.to_le_bytes().to_vec(),
        )?;
    }
    store_sync_event(
        database,
        transaction,
        request,
        &turn,
        attempt.as_ref(),
        revision,
    )?;
    encode_response(&json!({"turn": turn, "attempt": attempt, "conversationRevision": revision}))
}

fn stage_created_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
    committed_at_ms: u64,
) -> io::Result<Value> {
    let text = |field: &str| {
        document
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("new Turn identity is malformed"))
    };
    let conversation_id = text("conversationId")?;
    let turn_id = text("turnId")?;
    let lane_id = text("laneId")?;
    let ordinal = document
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("new Turn ordinal is malformed"))?;
    let created_at_ms = document
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("new Turn timestamp is malformed"))?;
    let claim = global_identity_claim_key(transaction, TURN_ID_CLAIM_NAMESPACE, turn_id)?;
    if database.entity_get(transaction, &claim)?.is_some() {
        return Err(conflict("turn identity already exists"));
    }
    let key = turn_key(transaction, conversation_id, turn_id)?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(conversation_id, turn_id),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("new Turn cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: committed_at_ms,
        },
    )?;
    database.entity_put(
        transaction,
        claim,
        transaction.owner_user_id().to_be_bytes().to_vec(),
    )?;
    let stored = database
        .entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("staged Turn disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(transaction, conversation_id, lane_id, ordinal)?,
        stored,
    )?;
    put_lane_compaction_index(database, transaction, document)?;
    database.entity_put(
        transaction,
        updated_index_key(transaction, conversation_id, created_at_ms, turn_id)?,
        encode_updated_index_value(turn_id, 1)?,
    )?;
    if lane_id == "main" {
        let activity = effective_activity_timestamp(
            document
                .get("projection")
                .and_then(Value::as_object)
                .and_then(|p| p.get("timestamp")),
            created_at_ms,
        );
        database.entity_put(
            transaction,
            activity_index_key(transaction, conversation_id, ordinal)?,
            encode_activity_index_value(activity),
        )?;
    }
    let execution_epoch = document
        .get("_executionEpoch")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("new Turn execution epoch is malformed"))?;
    public_turn(document.clone(), execution_epoch)
}

fn stage_created_attempt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &Map<String, Value>,
    projection_bytes: usize,
    committed_at_ms: u64,
) -> io::Result<()> {
    let text = |field: &str| {
        document
            .get(field)
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("new attempt identity is malformed"))
    };
    let attempt_id = text("attemptId")?;
    let conversation_id = text("conversationId")?;
    let turn_id = text("turnId")?;
    let command_id = text("commandId")?;
    let created_at_ms = document
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("new attempt timestamp is malformed"))?;
    let claim = global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)?;
    if database.entity_get(transaction, &claim)?.is_some() {
        return Err(conflict("attempt identity already exists"));
    }
    let command = attempt_command_key(transaction, conversation_id, command_id)?;
    if database.entity_get(transaction, &command)?.is_some() {
        return Err(conflict("attempt command identity already exists"));
    }
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: attempt_key(transaction, conversation_id, attempt_id)?,
            namespace: "generation_attempts".to_owned(),
            logical_key: attempt_id.to_owned(),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("new attempt cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: committed_at_ms,
        },
    )?;
    append_attempt_turn_directory(
        database,
        transaction,
        conversation_id,
        turn_id,
        None,
        document,
    )?;
    database.entity_put(
        transaction,
        claim,
        encode_attempt_locator(transaction.owner_user_id(), conversation_id)?,
    )?;
    database.entity_put(transaction, command, attempt_id.as_bytes().to_vec())?;
    database.entity_put(
        transaction,
        attempt_turn_count_key(transaction, conversation_id, turn_id)?,
        1_u64.to_le_bytes().to_vec(),
    )?;
    put_attempt_timing_indexes(database, transaction, document)?;
    if attempt_is_live(document) {
        database.entity_put(
            transaction,
            recovery_index_key(transaction, created_at_ms, attempt_id)?,
            recovery_index_value(document, projection_bytes)?,
        )?;
    } else if let Some(settled_at_ms) = terminal_attempt_settled_at(document)? {
        database.entity_put(
            transaction,
            attempt_event_retention_index_key(transaction, settled_at_ms, attempt_id)?,
            attempt_event_retention_index_value(document, 1)?,
        )?;
    }
    Ok(())
}

pub(crate) fn create_pair(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CreatePairRequest,
) -> io::Result<Vec<u8>> {
    let command_key =
        attempt_command_key(transaction, &request.conversation_id, &request.command_id)?;
    if let Some(existing_id) = database.entity_get(transaction, &command_key)? {
        let existing_id = std::str::from_utf8(&existing_id)
            .map_err(|_| invalid_data("attempt command identity is malformed"))?;
        let loaded = load_attempt_for_update(database, transaction, existing_id)?
            .ok_or_else(|| invalid_data("attempt command target is missing"))?;
        let output_turn_id = loaded
            .document
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?;
        let output = get(
            database,
            transaction,
            &request.conversation_id,
            output_turn_id,
        )?
        .ok_or_else(|| invalid_data("attempt Turn is missing"))?;
        let output: Value = serde_json::from_slice(&output)
            .map_err(|_| invalid_data("Turn response is malformed"))?;
        let submitted = match output.get("parentTurnId").and_then(Value::as_str) {
            Some(turn_id) => get(database, transaction, &request.conversation_id, turn_id)?
                .map(|bytes| {
                    serde_json::from_slice::<Value>(&bytes)
                        .map_err(|_| invalid_data("Turn response is malformed"))
                })
                .transpose()?,
            None => None,
        };
        let attempt = public_attempt(&loaded.document)?;
        let queue_id = loaded
            .document
            .get("_queueId")
            .and_then(Value::as_str)
            .unwrap_or("");
        let queue_pending =
            loaded.document.get("_queueState").and_then(Value::as_str) == Some("pending");
        let mut result = json!({
            "submittedTurn": submitted,
            "turn": output,
            "attempt": attempt,
            "conversationRevision": crate::conversation_header::revision(database, transaction, &request.conversation_id)?,
            "streamCursor": 1,
            "idempotentReplay": true,
            "_needsStart": attempt.get("status").and_then(Value::as_str) == Some("pending")
                && attempt.get("taskId").and_then(Value::as_str) == Some("") && !queue_pending
        });
        if queue_pending {
            if let Some(mut item) = crate::queue::item_by_id(database, transaction, queue_id)? {
                if let Some(map) = item.as_object_mut() {
                    map.remove("deduped");
                }
                result["queued"] = Value::Bool(true);
                result["queueId"] = Value::String(queue_id.to_owned());
                result["position"] = item.get("position").cloned().unwrap_or(Value::Null);
                result["queueItem"] = item;
            }
        }
        return encode_response(&result);
    }

    crate::conversation_header::ensure_for_turn(
        database,
        transaction,
        &request.conversation_id,
        &request.defaults,
        request.created_at_ms,
        request.committed_at_ms,
    )?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    if let Some(parent_id) = &request.parent_turn_id {
        if get(database, transaction, &request.conversation_id, parent_id)?.is_none() {
            return Err(typed_conflict(TurnConflictKind::ParentInvalid));
        }
    }
    if ((request.input_actor == "human" && request.queue_binding.is_none())
        || request.require_lane_idle)
        && lane_has_live_attempt(
            database,
            transaction,
            &request.conversation_id,
            &request.lane_id,
        )?
    {
        return Err(typed_conflict(TurnConflictKind::InProgress));
    }
    let head_key = entity_key(
        transaction,
        TURN_LANE_HEAD_NAMESPACE,
        &lane_prefix(&request.conversation_id, &request.lane_id)?,
    )?;
    let previous_ordinal = database
        .entity_get(transaction, &head_key)?
        .map(|value| decode_u64(Some(value), "turn lane head is malformed"))
        .transpose()?;
    let input_ordinal = previous_ordinal
        .map(|value| {
            value
                .checked_add(1)
                .ok_or_else(|| invalid_data("turn lane ordinal overflow"))
        })
        .transpose()?
        .unwrap_or(0);
    let previous_tail = if previous_ordinal.is_some() {
        let prefix = lane_prefix(&request.conversation_id, &request.lane_id)?;
        let (start, _) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            TURN_LANE_INDEX_NAMESPACE,
            &prefix,
        )?;
        let end = lane_index_key(
            transaction,
            &request.conversation_id,
            &request.lane_id,
            input_ordinal,
        )?;
        database
            .entity_scan_reverse(transaction, &start, &end, 1)?
            .pop()
            .map(|(_, stored)| materialize_turn(database, transaction, &stored))
            .transpose()?
            .and_then(|turn| {
                turn.get("turnId")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            })
    } else {
        None
    };
    if request.require_parent_is_lane_tail
        && previous_tail.as_ref() != request.parent_turn_id.as_ref()
    {
        return Err(typed_conflict(TurnConflictKind::LaneAdvanced));
    }
    if request.reject_if_human_queued
        && crate::queue::contains_kind(database, transaction, &request.conversation_id, "real")?
    {
        return Err(typed_conflict(TurnConflictKind::SupersededByHuman));
    }
    if request.input_actor == "human" {
        let _ = crate::queue::execute(
            database,
            transaction,
            &crate::queue::Request::KindClear {
                conversation_id: request.conversation_id.clone(),
                kind: "goal_continuation".to_owned(),
            },
        )?;
    }
    let output_ordinal = input_ordinal
        .checked_add(1)
        .ok_or_else(|| invalid_data("turn lane ordinal overflow"))?;
    let lane_count_key = lane_count_key(transaction, &request.conversation_id, &request.lane_id)?;
    let lane_count = decode_u64(
        database.entity_get(transaction, &lane_count_key)?,
        "turn lane count is malformed",
    )?
    .checked_add(2)
    .ok_or_else(|| invalid_data("turn lane count overflow"))?;
    let submitted_settlement = json!({
        "outcome": "completed",
        "cause": if request.input_actor == "human" { "submitted" } else { "orchestration_generated" },
        "resumeOptions": []
    });
    let input_attempt_value = request.input_attempt_id.as_ref().map(|attempt_id| json!({
        "attemptId": attempt_id, "conversationId": request.conversation_id, "turnId": request.input_turn_id,
        "commandId": format!("{}:input", request.command_id), "taskId": "", "operation": "generate",
        "status": "completed", "baseProjectionRevision": 0, "resumeAnchor": {},
        "createdAt": request.created_at_ms, "startedAt": request.created_at_ms, "settledAt": request.created_at_ms,
        "_dispatchMode": "", "_queueId": "", "_queueState": "", "_config": {"runId": request.run_id}, "_error": {}
    }));
    let input_document = Map::from_iter([
        ("turnId".into(), json!(request.input_turn_id)),
        (
            "presentationId".into(),
            json!(request.input_presentation_id),
        ),
        ("conversationId".into(), json!(request.conversation_id)),
        ("laneId".into(), json!(request.lane_id)),
        (
            "parentTurnId".into(),
            request
                .parent_turn_id
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        ),
        ("ordinal".into(), json!(input_ordinal)),
        ("actor".into(), json!(request.input_actor)),
        ("kind".into(), json!(request.input_kind)),
        ("runId".into(), json!(request.run_id)),
        ("status".into(), json!("completed")),
        (
            "currentAttemptId".into(),
            request
                .input_attempt_id
                .clone()
                .map(Value::String)
                .unwrap_or(Value::Null),
        ),
        (
            "projection".into(),
            Value::Object(request.input_projection.clone()),
        ),
        ("projectionRevision".into(), json!(1)),
        ("settlement".into(), submitted_settlement.clone()),
        ("createdAt".into(), json!(request.created_at_ms)),
        ("updatedAt".into(), json!(request.created_at_ms)),
        ("_executionEpoch".into(), json!(execution_epoch)),
    ]);
    let output_projection = json!({"content":"", "thinking":"", "segments":[], "toolRounds":[]});
    let output_document = Map::from_iter([
        ("turnId".into(), json!(request.output_turn_id)),
        (
            "presentationId".into(),
            json!(request.output_presentation_id),
        ),
        ("conversationId".into(), json!(request.conversation_id)),
        ("laneId".into(), json!(request.lane_id)),
        ("parentTurnId".into(), json!(request.input_turn_id)),
        ("ordinal".into(), json!(output_ordinal)),
        ("actor".into(), json!(request.output_actor)),
        ("kind".into(), json!(request.output_kind)),
        ("runId".into(), json!(request.run_id)),
        ("status".into(), json!("pending")),
        ("currentAttemptId".into(), json!(request.output_attempt_id)),
        ("projection".into(), output_projection.clone()),
        ("projectionRevision".into(), json!(1)),
        ("settlement".into(), json!({})),
        ("createdAt".into(), json!(request.created_at_ms)),
        ("updatedAt".into(), json!(request.created_at_ms)),
        ("_executionEpoch".into(), json!(execution_epoch)),
    ]);
    let queue_id = request
        .queue_binding
        .as_ref()
        .map(|value| value.queue_id.as_str())
        .unwrap_or("");
    let output_attempt = json!({
        "attemptId": request.output_attempt_id, "conversationId": request.conversation_id, "turnId": request.output_turn_id,
        "commandId": request.command_id, "taskId": "", "operation": "generate", "status": "pending",
        "baseProjectionRevision": 0, "resumeAnchor": {}, "createdAt": request.created_at_ms,
        "startedAt": null, "settledAt": null, "_dispatchMode": request.dispatch_mode,
        "_queueId": queue_id, "_queueState": if queue_id.is_empty() { "" } else { "pending" },
        "_config": request.config, "_error": {}
    });
    let output_attempt_document = output_attempt
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("attempt cannot be encoded"))?;
    let submitted = stage_created_turn(
        database,
        transaction,
        &input_document,
        request.committed_at_ms,
    )?;
    let output = stage_created_turn(
        database,
        transaction,
        &output_document,
        request.committed_at_ms,
    )?;
    if let Some(input_attempt) = input_attempt_value.as_ref().and_then(Value::as_object) {
        stage_created_attempt(
            database,
            transaction,
            input_attempt,
            serde_json::to_vec(&request.input_projection).unwrap().len(),
            request.committed_at_ms,
        )?;
        append_attempt_event(
            database,
            transaction,
            AttemptEventAppend {
                conversation_id: &request.conversation_id,
                turn_id: &request.input_turn_id,
                attempt_id: request.input_attempt_id.as_deref().unwrap(),
                projection_revision: 1,
                event_type: "terminal_settlement",
                payload: json!({"status":"completed", "settlement":submitted_settlement, "projection":request.input_projection}),
                occurred_at_ms: request.created_at_ms,
                publish_conversation_sync: false,
            },
        )?;
    }
    stage_created_attempt(
        database,
        transaction,
        &output_attempt_document,
        serde_json::to_vec(&output_projection).unwrap().len(),
        request.committed_at_ms,
    )?;
    if queue_id.is_empty() {
        set_lane_live_attempt(
            database,
            transaction,
            &request.conversation_id,
            &request.lane_id,
            &request.output_turn_id,
            &request.output_attempt_id,
            true,
        )?;
        if request.dispatch_mode == "conversation_executor" {
            database.entity_put(
                transaction,
                dispatchable_index_key(
                    transaction,
                    request.created_at_ms,
                    &request.output_attempt_id,
                )?,
                dispatchable_index_value(
                    transaction.owner_user_id(),
                    &request.conversation_id,
                    &request.output_turn_id,
                    &request.output_attempt_id,
                )?,
            )?;
        }
    } else {
        database.entity_delete(
            transaction,
            recovery_index_key(
                transaction,
                request.created_at_ms,
                &request.output_attempt_id,
            )?,
        )?;
    }
    let mut queue_item = None;
    if let Some(binding) = &request.queue_binding {
        let mut value = crate::queue::enqueue_item(
            database,
            transaction,
            &crate::queue::EnqueueItemRequest {
                conversation_id: request.conversation_id.clone(),
                queue_id: binding.queue_id.clone(),
                kind: binding.kind.clone(),
                priority: binding.priority,
                message: binding.message.clone(),
                config: request.config.clone(),
                created_at_ms: binding.created_at_ms,
                input_turn_id: request.input_turn_id.clone(),
                output_turn_id: request.output_turn_id.clone(),
                attempt_id: request.output_attempt_id.clone(),
                dedupe_by_kind: false,
                include_documents: true,
            },
        )?;
        if let Some(map) = value.as_object_mut() {
            map.remove("deduped");
        }
        queue_item = Some(value);
    }
    append_attempt_event(
        database,
        transaction,
        AttemptEventAppend {
            conversation_id: &request.conversation_id,
            turn_id: &request.output_turn_id,
            attempt_id: &request.output_attempt_id,
            projection_revision: 1,
            event_type: "status_changed",
            payload: json!({"status":"pending"}),
            occurred_at_ms: request.created_at_ms,
            publish_conversation_sync: false,
        },
    )?;
    database.entity_put(transaction, head_key, output_ordinal.to_le_bytes().to_vec())?;
    database.entity_put(
        transaction,
        lane_count_key,
        lane_count.to_le_bytes().to_vec(),
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    let revision = crate::conversation_header::advance_for_turns(
        database,
        transaction,
        &request.conversation_id,
        request.created_at_ms,
        request.committed_at_ms,
        if request.lane_id == "main" { 2 } else { 0 },
    )?;
    let public_output_attempt = public_attempt(&output_attempt_document)?;
    store_turn_upsert_sync_event(
        database,
        transaction,
        &request.conversation_id,
        vec![submitted.clone(), output.clone()],
        vec![public_output_attempt.clone()],
        queue_item.clone().into_iter().collect(),
        Vec::new(),
        revision,
        request.created_at_ms,
        request.committed_at_ms,
        Some(&request.output_turn_id),
        Some(&request.output_attempt_id),
    )?;
    let mut result = json!({"submittedTurn":submitted, "turn":output, "attempt":public_output_attempt,
        "conversationRevision":revision, "streamCursor":1, "idempotentReplay":false, "_needsStart":queue_id.is_empty()});
    if let Some(item) = queue_item {
        result["queued"] = Value::Bool(true);
        result["queueId"] = Value::String(queue_id.to_owned());
        result["position"] = item.get("position").cloned().unwrap_or(Value::Null);
        result["queueItem"] = item;
    }
    encode_response(&result)
}

fn queued_turn_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Map<String, Value>> {
    let stored = database
        .entity_get(
            transaction,
            &turn_key(transaction, conversation_id, turn_id)?,
        )?
        .ok_or_else(|| invalid_data("queued Turn pair is incomplete"))?;
    let document = materialize_turn(database, transaction, &stored)?;
    if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
        || document.get("turnId").and_then(Value::as_str) != Some(turn_id)
    {
        return Err(invalid_data("queued Turn identity differs"));
    }
    Ok(document)
}

fn queued_delete_row(document: &Map<String, Value>) -> io::Result<DeleteTurnRow> {
    Ok(DeleteTurnRow {
        lane_id: document
            .get("laneId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("queued Turn lane identity is malformed"))?
            .to_owned(),
        ordinal: document
            .get("ordinal")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("queued Turn ordinal is malformed"))?,
        updated_at_ms: document
            .get("updatedAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("queued Turn timestamp is malformed"))?,
        attempt_id: document
            .get("currentAttemptId")
            .and_then(Value::as_str)
            .map(str::to_owned),
    })
}

pub(crate) fn queue_activate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &QueueTransitionRequest,
) -> io::Result<Vec<u8>> {
    let binding = crate::queue::remove_turn_item(
        database,
        transaction,
        &request.conversation_id,
        &request.queue_id,
    )?
    .filter(|binding| !binding.attempt_id.is_empty())
    .ok_or_else(|| not_found("queued Turn not found"))?;
    let loaded = load_attempt_for_update(database, transaction, &binding.attempt_id)?
        .ok_or_else(|| conflict("queued attempt is no longer pending"))?;
    if loaded.conversation_id != request.conversation_id
        || loaded.document.get("attemptId").and_then(Value::as_str)
            != Some(binding.attempt_id.as_str())
        || loaded.document.get("turnId").and_then(Value::as_str)
            != Some(binding.output_turn_id.as_str())
        || loaded.document.get("_queueId").and_then(Value::as_str)
            != Some(request.queue_id.as_str())
        || loaded.document.get("_queueState").and_then(Value::as_str) != Some("pending")
        || loaded.document.get("status").and_then(Value::as_str) != Some("pending")
        || loaded.document.get("taskId").and_then(Value::as_str) != Some("")
    {
        return Err(conflict("queued attempt is no longer pending"));
    }
    if binding.input_turn_id.is_empty() || binding.output_turn_id.is_empty() {
        return Err(invalid_data("queued Turn pair is incomplete"));
    }
    let input_document = queued_turn_document(
        database,
        transaction,
        &request.conversation_id,
        &binding.input_turn_id,
    )?;
    let output_document = queued_turn_document(
        database,
        transaction,
        &request.conversation_id,
        &binding.output_turn_id,
    )?;
    if output_document
        .get("currentAttemptId")
        .and_then(Value::as_str)
        != Some(binding.attempt_id.as_str())
    {
        return Err(invalid_data("queued Turn pair differs from its attempt"));
    }
    let lane_id = output_document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("queued Turn lane identity is malformed"))?;
    if lane_has_live_attempt(database, transaction, &request.conversation_id, lane_id)? {
        return Err(typed_conflict(TurnConflictKind::InProgress));
    }
    let projection_bytes = serde_json::to_vec(
        output_document
            .get("projection")
            .ok_or_else(|| invalid_data("queued Turn projection is missing"))?,
    )
    .map_err(|_| invalid_data("queued Turn projection cannot be encoded"))?
    .len();
    let mut attempt = loaded.document.clone();
    attempt.insert("_queueId".to_owned(), Value::String(String::new()));
    attempt.insert("_queueState".to_owned(), Value::String(String::new()));
    let created_at_ms = attempt
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("queued attempt timestamp is malformed"))?;
    database.entity_put(
        transaction,
        recovery_index_key(transaction, created_at_ms, &binding.attempt_id)?,
        recovery_index_value(&attempt, projection_bytes)?,
    )?;
    store_attempt_document(
        database,
        transaction,
        &binding.attempt_id,
        &loaded,
        &attempt,
        request.committed_at_ms,
    )?;
    set_lane_live_attempt(
        database,
        transaction,
        &request.conversation_id,
        lane_id,
        &binding.output_turn_id,
        &binding.attempt_id,
        true,
    )?;
    if attempt.get("_dispatchMode").and_then(Value::as_str) == Some("conversation_executor") {
        database.entity_put(
            transaction,
            dispatchable_index_key(transaction, created_at_ms, &binding.attempt_id)?,
            dispatchable_index_value(
                transaction.owner_user_id(),
                &request.conversation_id,
                &binding.output_turn_id,
                &binding.attempt_id,
            )?,
        )?;
    }
    let revision = crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &request.conversation_id,
        request.committed_at_ms,
        request.committed_at_ms,
        false,
    )?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let submitted = public_turn(input_document, execution_epoch)?;
    let output = public_turn(output_document, execution_epoch)?;
    let public_attempt = public_attempt(&attempt)?;
    store_turn_upsert_sync_event(
        database,
        transaction,
        &request.conversation_id,
        vec![submitted.clone(), output.clone()],
        vec![public_attempt.clone()],
        Vec::new(),
        vec![request.queue_id.clone()],
        revision,
        request.committed_at_ms,
        request.committed_at_ms,
        Some(&binding.output_turn_id),
        Some(&binding.attempt_id),
    )?;
    encode_response(&json!({
        "submittedTurn": submitted,
        "turn": output,
        "attempt": public_attempt,
        "conversationRevision": revision,
        "streamCursor": 1,
        "idempotentReplay": false,
        "queued": false,
        "queueId": request.queue_id,
        "_needsStart": true
    }))
}

pub(crate) fn queue_cancel(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &QueueTransitionRequest,
) -> io::Result<Vec<u8>> {
    if !crate::conversation_header::active_exists(database, transaction, &request.conversation_id)?
    {
        return Err(not_found("conversation not found"));
    }
    let previous_revision =
        crate::conversation_header::revision(database, transaction, &request.conversation_id)?;
    let Some(binding) = crate::queue::remove_turn_item(
        database,
        transaction,
        &request.conversation_id,
        &request.queue_id,
    )?
    else {
        return encode_response(&json!({
            "conversationId": request.conversation_id,
            "conversationRevision": previous_revision,
            "queueId": request.queue_id,
            "cancelled": false,
            "inputTurn": Value::Null,
            "deletedTurnIds": []
        }));
    };
    if binding.input_turn_id.is_empty()
        || binding.output_turn_id.is_empty()
        || binding.attempt_id.is_empty()
    {
        return Err(conflict("legacy queue row is not cancellable"));
    }
    let loaded = load_attempt_for_update(database, transaction, &binding.attempt_id)?
        .ok_or_else(|| conflict("queued attempt is no longer cancellable"))?;
    if loaded.conversation_id != request.conversation_id
        || loaded.document.get("turnId").and_then(Value::as_str)
            != Some(binding.output_turn_id.as_str())
        || loaded.document.get("_queueId").and_then(Value::as_str)
            != Some(request.queue_id.as_str())
        || loaded.document.get("_queueState").and_then(Value::as_str) != Some("pending")
        || loaded.document.get("status").and_then(Value::as_str) != Some("pending")
        || loaded.document.get("taskId").and_then(Value::as_str) != Some("")
    {
        return Err(conflict("queued attempt is no longer cancellable"));
    }
    let input_document = queued_turn_document(
        database,
        transaction,
        &request.conversation_id,
        &binding.input_turn_id,
    )?;
    let output_document = queued_turn_document(
        database,
        transaction,
        &request.conversation_id,
        &binding.output_turn_id,
    )?;
    if output_document
        .get("currentAttemptId")
        .and_then(Value::as_str)
        != Some(binding.attempt_id.as_str())
    {
        return Err(invalid_data("queued Turn pair differs from its attempt"));
    }
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let input_turn = public_turn(input_document.clone(), execution_epoch)?;
    let mut rows = BTreeMap::new();
    rows.insert(
        binding.input_turn_id.clone(),
        queued_delete_row(&input_document)?,
    );
    rows.insert(
        binding.output_turn_id.clone(),
        queued_delete_row(&output_document)?,
    );
    let (deleted_turn_ids, deleted_main_turns) = apply_deletion_rows(
        database,
        transaction,
        &request.conversation_id,
        &rows,
        request.committed_at_ms,
    )?;
    let revision = crate::conversation_header::advance_for_turn_delete(
        database,
        transaction,
        &request.conversation_id,
        deleted_main_turns,
        request.committed_at_ms,
        request.committed_at_ms,
    )?;
    store_delete_sync_event(
        database,
        transaction,
        &request.conversation_id,
        &deleted_turn_ids,
        std::slice::from_ref(&request.queue_id),
        revision,
        request.committed_at_ms,
    )?;
    encode_response(&json!({
        "conversationId": request.conversation_id,
        "conversationRevision": revision,
        "queueId": request.queue_id,
        "cancelled": true,
        "inputTurn": input_turn,
        "deletedTurnIds": deleted_turn_ids
    }))
}

pub(crate) fn update_projection(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ProjectionUpdateRequest,
) -> io::Result<Vec<u8>> {
    let staged = stage_projection_update(
        database,
        transaction,
        request,
        ProjectionUpdateMode::TypedSettledEdit,
    )?;
    let conversation_revision = crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &request.conversation_id,
        request.updated_at_ms,
        request.committed_at_ms,
        false,
    )?;
    store_projection_sync_event(
        database,
        transaction,
        request,
        &staged.before,
        &staged.after,
        conversation_revision,
        &[],
    )?;
    encode_response(&json!({
        "turn": staged.turn,
        "conversationRevision": conversation_revision
    }))
}

pub(crate) fn steer_commit(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &SteerCommitRequest,
) -> io::Result<Vec<u8>> {
    let closed = || conflict("The steer execution window is already closed");
    let Some(attempt) = load_attempt_for_update(database, transaction, &request.attempt_id)? else {
        return Err(closed());
    };
    if attempt.conversation_id != request.conversation_id
        || !matches!(
            attempt.document.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        )
        || attempt.document.get("_queueState").and_then(Value::as_str) != Some("")
        || attempt
            .document
            .get("taskId")
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
    {
        return Err(closed());
    }
    let Some(turn) = load_current_turn_for_attempt(
        database,
        transaction,
        &request.attempt_id,
        &attempt.document,
        &request.conversation_id,
    )?
    else {
        return Err(closed());
    };
    if !matches!(
        turn.document.get("status").and_then(Value::as_str),
        Some("pending" | "running")
    ) {
        return Err(closed());
    }
    let turn_id = turn
        .document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn identity is malformed"))?
        .to_owned();
    let base_revision = turn
        .document
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let previous_projection = turn
        .document
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let mut next_projection = previous_projection.clone();
    let mut records = next_projection
        .get("_userSteerInjects")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let block_id = format!("injection:user-steer:{}", request.command_id);
    if !records
        .iter()
        .any(|record| record.get("blockId").and_then(Value::as_str) == Some(block_id.as_str()))
    {
        records.push(json!({
            "blockId": block_id,
            "commandId": request.command_id,
            "count": 1,
            "previews": [{"text": request.text.chars().take(1200).collect::<String>()}],
            "deliveryState": "pending"
        }));
    }
    next_projection.insert("_userSteerInjects".to_owned(), Value::Array(records));
    let projection_request = ProjectionUpdateRequest {
        conversation_id: request.conversation_id.clone(),
        turn_id,
        projection_json: serde_json::to_vec(&next_projection)
            .map_err(|_| invalid_data("turn projection cannot be encoded"))?,
        expected_projection_revision: base_revision,
        updated_at_ms: request.updated_at_ms,
        committed_at_ms: request.committed_at_ms,
    };
    let staged = stage_projection_update(
        database,
        transaction,
        &projection_request,
        ProjectionUpdateMode::FencedLiveSteer,
    )?;
    let conversation_revision = crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &request.conversation_id,
        request.updated_at_ms,
        request.committed_at_ms,
        false,
    )?;
    store_projection_sync_event(
        database,
        transaction,
        &projection_request,
        &staged.before,
        &staged.after,
        conversation_revision,
        &[],
    )?;
    encode_response(&json!({
        "steered": true,
        "injectionId": request.command_id,
        "blockId": block_id,
        "turn": staged.turn,
        "conversationRevision": conversation_revision
    }))
}

fn visible_flow_kind(kind: &str) -> bool {
    ["flow_", "autopilot_", "endpoint_"]
        .iter()
        .any(|prefix| kind.starts_with(prefix))
}

fn visible_execution_identity(round: &Map<String, Value>) -> (String, String) {
    let text = |primary: &str, legacy: &str| {
        round
            .get(primary)
            .or_else(|| round.get(legacy))
            .and_then(|value| match value {
                Value::String(value) => Some(value.clone()),
                Value::Number(value) => Some(value.to_string()),
                _ => None,
            })
            .map(|value| value.trim().chars().take(256).collect::<String>())
            .filter(|value| !value.starts_with("@dispatching:"))
            .unwrap_or_default()
    };
    (text("attemptId", "_attemptId"), text("taskId", "_taskId"))
}

fn visible_llm_round(round: &Map<String, Value>) -> Option<Value> {
    match round.get("llmRound") {
        Some(Value::Number(value)) if value.as_i64().is_some() => {
            Some(Value::Number(value.clone()))
        }
        Some(Value::String(value)) if !value.trim().is_empty() && value.chars().count() <= 64 => {
            Some(Value::String(value.trim().to_owned()))
        }
        _ => None,
    }
}

fn visible_round_is_synthetic(round: &Map<String, Value>) -> bool {
    [
        "_inboxInject",
        "_peerInject",
        "_userSteerInject",
        "_stallNudge",
        "_programSynthetic",
    ]
    .iter()
    .any(|field| round.get(*field).is_some_and(json_truthy))
}

fn visible_tool_segment(round: &Map<String, Value>, position: usize) -> Value {
    let call_id = round
        .get("toolCallId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    let (attempt_id, task_id) = visible_execution_identity(round);
    let llm_round = visible_llm_round(round);
    let suffix = if let Some(llm_round) = &llm_round {
        let scope = if attempt_id.is_empty() {
            task_id.as_str()
        } else {
            attempt_id.as_str()
        };
        let scope = if scope.is_empty() {
            String::new()
        } else {
            format!("attempt-{scope}:")
        };
        format!("{scope}llm-{llm_round}")
    } else if let Some(round_number) = round.get("roundNum") {
        let scope = if attempt_id.is_empty() {
            task_id.as_str()
        } else {
            attempt_id.as_str()
        };
        let scope = if scope.is_empty() {
            String::new()
        } else {
            format!("attempt-{scope}:")
        };
        format!("{scope}round-{round_number}")
    } else {
        format!("legacy-{position}")
    };
    let mut segment = Map::from_iter([
        ("type".to_owned(), json!("tool_use")),
        (
            "blockId".to_owned(),
            json!(if call_id.is_empty() {
                format!("tool:{suffix}")
            } else {
                format!("tool:{call_id}")
            }),
        ),
        ("id".to_owned(), json!(call_id)),
        (
            "name".to_owned(),
            round.get("toolName").cloned().unwrap_or_else(|| json!("")),
        ),
        (
            "input".to_owned(),
            round.get("toolArgs").cloned().unwrap_or_else(|| json!("")),
        ),
        ("llmRound".to_owned(), llm_round.unwrap_or(Value::Null)),
        (
            "result".to_owned(),
            json!({
                "content": round.get("toolContent").cloned().unwrap_or(Value::Null),
                "status": round.get("status").cloned().unwrap_or(Value::Null)
            }),
        ),
    ]);
    if !attempt_id.is_empty() {
        segment.insert("attemptId".to_owned(), json!(attempt_id));
    }
    if !task_id.is_empty() {
        segment.insert("taskId".to_owned(), json!(task_id));
    }
    Value::Object(segment)
}

fn visible_stable_segments(projection: &mut Map<String, Value>, actor: &str) {
    let rounds = projection
        .get("toolRounds")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let mut segments = Vec::new();
    let mut previous_batch: Option<(String, String)> = None;
    for (position, value) in rounds.iter().enumerate() {
        let Some(round) = value.as_object() else {
            continue;
        };
        if visible_round_is_synthetic(round) {
            continue;
        }
        let (attempt_id, task_id) = visible_execution_identity(round);
        let scope = if attempt_id.is_empty() {
            task_id
        } else {
            attempt_id
        };
        let batch = (
            scope.clone(),
            visible_llm_round(round)
                .map(|value| value.to_string())
                .unwrap_or_else(|| format!("position-{position}")),
        );
        if previous_batch.as_ref() != Some(&batch) {
            previous_batch = Some(batch.clone());
            let suffix = if let Some(llm_round) = visible_llm_round(round) {
                if scope.is_empty() {
                    format!("llm-{llm_round}")
                } else {
                    format!("attempt-{scope}:llm-{llm_round}")
                }
            } else if let Some(round_number) = round.get("roundNum") {
                if scope.is_empty() {
                    format!("round-{round_number}")
                } else {
                    format!("attempt-{scope}:round-{round_number}")
                }
            } else {
                format!("legacy-{position}")
            };
            for (field, segment_type) in [("thinking", "thinking"), ("assistantContent", "text")] {
                if let Some(text) = round
                    .get(field)
                    .and_then(Value::as_str)
                    .filter(|v| !v.is_empty())
                {
                    let mut segment = json!({
                        "type": segment_type,
                        "text": text,
                        "blockId": format!("{segment_type}:{suffix}"),
                        "deliverable": false,
                        "llmRound": visible_llm_round(round).unwrap_or(Value::Null)
                    });
                    if !scope.is_empty() {
                        segment["attemptId"] = json!(scope);
                    }
                    segments.push(segment);
                }
            }
        }
        segments.push(visible_tool_segment(round, position));
    }
    let thinking = projection
        .get("thinking")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !thinking.is_empty() {
        segments.push(json!({
            "type": "thinking", "blockId": "thinking:terminal", "text": thinking,
            "deliverable": false, "terminal": true
        }));
    }
    let content = projection
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !content.is_empty() || actor != "human" {
        segments.push(json!({
            "type": "text", "blockId": "text:terminal", "text": content,
            "deliverable": true, "terminal": true
        }));
    }
    projection.insert("segments".to_owned(), Value::Array(segments));
    if !projection.contains_key("fileChanges") {
        if let Some(files) = projection
            .get("modifiedFileList")
            .and_then(Value::as_array)
            .filter(|files| !files.is_empty())
        {
            let declared = projection
                .get("modifiedFiles")
                .and_then(Value::as_u64)
                .unwrap_or(0);
            projection.insert(
                "fileChanges".to_owned(),
                json!({
                    "blockId": "file-changes",
                    "count": declared.max(files.len() as u64),
                    "state": "applied",
                    "files": files
                }),
            );
        }
    }
}

fn visible_shape(
    raw: &Map<String, Value>,
    default_kind: &str,
    live_projection: &Map<String, Value>,
) -> (String, String, Map<String, Value>) {
    let mut message = raw.clone();
    for (legacy, current) in [
        ("_isEndpointPlanner", "_isFlowPlanner"),
        ("_isEndpointReview", "_isFlowReview"),
        ("_epIteration", "_flowIteration"),
        ("_epPlannerIteration", "_flowPlannerIteration"),
        ("_epApproved", "_flowApproved"),
        ("_epNextPhase", "_flowNextPhase"),
        ("_epStateChangingCount", "_flowStateChangingCount"),
    ] {
        if !message.contains_key(current) {
            if let Some(value) = message.get(legacy).cloned() {
                message.insert(current.to_owned(), value);
            }
        }
        message.remove(legacy);
    }
    let role = message.get("role").and_then(Value::as_str).unwrap_or("");
    let (actor, kind) = if message.get("_isVirtualUser").is_some_and(json_truthy) {
        ("virtual_user", "autopilot_virtual_user")
    } else if message.get("_isFlowReview").is_some_and(json_truthy) {
        ("critic", "flow_node")
    } else if message.get("_isFlowPlanner").is_some_and(json_truthy) {
        ("planner", "flow_node")
    } else if message.get("_flowNodeId").is_some_and(json_truthy)
        || message.get("_flowRunId").is_some_and(json_truthy)
    {
        (
            if role == "user" {
                "critic"
            } else {
                "assistant"
            },
            "flow_node",
        )
    } else {
        (
            if role == "user" {
                "critic"
            } else {
                "assistant"
            },
            if default_kind.is_empty() {
                "flow_node"
            } else {
                default_kind
            },
        )
    };
    const PUBLIC_FIELDS: &[&str] = &[
        "content",
        "thinking",
        "toolRounds",
        "segments",
        "usage",
        "apiRounds",
        "cost",
        "lastRoundUsage",
        "model",
        "preset",
        "providerId",
        "routeSnapshot",
        "thinkingDepth",
        "modifiedFiles",
        "modifiedFileList",
        "fileChanges",
        "todoState",
        "waitingOn",
        "fallbackModel",
        "fallbackFrom",
        "fallbackReason",
        "fallbackKind",
        "translatedContent",
        "originalContent",
        "translation",
        "timestamp",
        "images",
        "attachments",
        "videos",
        "pdfTexts",
        "convRefs",
        "replyQuotes",
        "_branchLanes",
        "orchestration",
        "provenance",
        "_inboxInjects",
        "_peerInjects",
        "_userSteerInjects",
        "_stallNudges",
        "origin",
        "contextSnapshot",
        "compaction",
        "imageGeneration",
        "proposedPlan",
        "planExecution",
        "activityTimeline",
        "timingTrace",
        "rolledBack",
    ];
    let mut projection = Map::new();
    for field in PUBLIC_FIELDS {
        if let Some(value) = message.get(*field) {
            projection.insert((*field).to_owned(), value.clone());
        }
    }
    if !projection.contains_key("providerId") {
        if let Some(value) = message.get("provider_id") {
            projection.insert("providerId".to_owned(), value.clone());
        }
    }
    if !projection.contains_key("content") {
        projection.insert(
            "content".to_owned(),
            message
                .get("text")
                .cloned()
                .filter(|v| !v.is_null())
                .unwrap_or_else(|| json!("")),
        );
    }
    projection
        .entry("thinking".to_owned())
        .or_insert_with(|| json!(""));
    projection
        .entry("toolRounds".to_owned())
        .or_insert_with(|| json!([]));
    if !projection
        .get("routeSnapshot")
        .is_some_and(Value::is_object)
    {
        let model_id = projection
            .get("model")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .chars()
            .take(256)
            .collect::<String>();
        let provider_id = projection
            .get("providerId")
            .and_then(Value::as_str)
            .unwrap_or("")
            .trim()
            .chars()
            .take(256)
            .collect::<String>();
        if !model_id.is_empty() || !provider_id.is_empty() {
            projection.insert(
                "routeSnapshot".to_owned(),
                json!({
                    "contract_version":"tofu.route-snapshot/v2",
                    "legacy":true,
                    "selected_model":null,
                    "provider_scoped_selection":null,
                    "preferred_provider_id":provider_id,
                    "actual_model":if model_id.is_empty() {
                        Value::Null
                    } else {
                        json!({"creator_id":"legacy", "model_id":model_id})
                    },
                    "provider_id":provider_id,
                    "offering_id":"",
                    "deployment_id":"",
                    "connection_id":"",
                    "credential":null,
                    "wire_model_id":model_id,
                    "transitions":[],
                    "degradation_reasons":["legacy_turn_projection"],
                    "recorded_at":0.0
                }),
            );
        }
    }
    if let Some(Value::Array(rounds)) = projection.get_mut("toolRounds") {
        for round in rounds {
            if let Some(round) = round.as_object_mut() {
                if round.get("results").is_some_and(Value::is_null) {
                    round.remove("results");
                }
            }
        }
    }
    if let (Some(Value::Array(rounds)), Some(Value::Array(live_rounds))) = (
        projection.get_mut("toolRounds"),
        live_projection.get("toolRounds"),
    ) {
        let live_by_id: BTreeMap<String, &Map<String, Value>> = live_rounds
            .iter()
            .filter_map(Value::as_object)
            .filter_map(|round| {
                round
                    .get("toolCallId")
                    .and_then(Value::as_str)
                    .filter(|value| !value.is_empty())
                    .map(|value| (value.to_owned(), round))
            })
            .collect();
        for round in rounds.iter_mut().filter_map(Value::as_object_mut) {
            let Some(live) = round
                .get("toolCallId")
                .and_then(Value::as_str)
                .and_then(|call_id| live_by_id.get(call_id))
            else {
                continue;
            };
            for field in [
                "toolArgs",
                "toolContent",
                "tStart",
                "tEnd",
                "attemptId",
                "taskId",
            ] {
                let missing = matches!(round.get(field), None | Some(Value::Null))
                    || round.get(field).and_then(Value::as_str) == Some("");
                if missing {
                    if let Some(value) = live
                        .get(field)
                        .filter(|value| !matches!(value, Value::Null) && value.as_str() != Some(""))
                    {
                        round.insert(field.to_owned(), value.clone());
                    }
                }
            }
        }
    }
    let mut orchestration = projection
        .get("orchestration")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if let Some(value) = message
        .get("_flowIteration")
        .filter(|value| json_truthy(value))
        .or_else(|| {
            message
                .get("_flowPlannerIteration")
                .filter(|value| json_truthy(value))
        })
    {
        orchestration.insert("iteration".to_owned(), value.clone());
    }
    for (target, source) in [
        ("approved", "_flowApproved"),
        ("nextPhase", "_flowNextPhase"),
        ("stuck", "_isStuck"),
        ("flowNodeId", "_flowNodeId"),
        ("flowRunId", "_flowRunId"),
    ] {
        if let Some(value) = message.get(source).filter(|value| !value.is_null()) {
            orchestration.insert(target.to_owned(), value.clone());
        }
    }
    projection.insert("orchestration".to_owned(), Value::Object(orchestration));
    visible_stable_segments(&mut projection, actor);
    (actor.to_owned(), kind.to_owned(), projection)
}

struct VisibleRootReplacement<'a> {
    actor: &'a str,
    kind: &'a str,
    run_id: &'a str,
    projection: &'a Map<String, Value>,
    updated_at_ms: u64,
    committed_at_ms: u64,
}

fn replace_visible_root(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    loaded: &LoadedTurnForAttempt,
    replacement: VisibleRootReplacement<'_>,
) -> io::Result<(Value, u64)> {
    let mut document = loaded.document.clone();
    let turn_id = document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("visible root Turn identity is malformed"))?
        .to_owned();
    let conversation_id = document
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("visible root conversation identity is malformed"))?
        .to_owned();
    let lane_id = document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("visible root lane identity is malformed"))?
        .to_owned();
    let ordinal = document
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("visible root ordinal is malformed"))?;
    let created_at_ms = document
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("visible root creation time is malformed"))?;
    let previous_updated_at_ms = document
        .get("updatedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("visible root update time is malformed"))?;
    let next_revision = document
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .and_then(|revision| revision.checked_add(1))
        .ok_or_else(|| invalid_data("visible root revision overflows"))?;
    retire_projection_head(
        database,
        transaction,
        &conversation_id,
        &turn_id,
        loaded.projection_head.as_ref(),
    )?;
    document.insert("actor".to_owned(), json!(replacement.actor));
    document.insert("kind".to_owned(), json!(replacement.kind));
    document.insert("runId".to_owned(), json!(replacement.run_id));
    document.insert(
        "projection".to_owned(),
        Value::Object(replacement.projection.clone()),
    );
    document.insert("projectionRevision".to_owned(), json!(next_revision));
    document.insert("updatedAt".to_owned(), json!(replacement.updated_at_ms));
    document.remove("_projectionHead");
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: loaded.key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&conversation_id, &turn_id),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("visible root Turn cannot be encoded"))?,
            expected_version: Some(loaded.physical_version),
            updated_at_ms: replacement.committed_at_ms,
        },
    )?;
    let stored = database
        .entity_get(transaction, &loaded.key)?
        .ok_or_else(|| invalid_data("visible root Turn disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(transaction, &conversation_id, &lane_id, ordinal)?,
        stored,
    )?;
    put_lane_compaction_index(database, transaction, &document)?;
    if lane_id == "main" {
        database.entity_put(
            transaction,
            activity_index_key(transaction, &conversation_id, ordinal)?,
            encode_activity_index_value(effective_activity_timestamp(
                replacement.projection.get("timestamp"),
                created_at_ms,
            )),
        )?;
    }
    database.entity_delete(
        transaction,
        updated_index_key(
            transaction,
            &conversation_id,
            previous_updated_at_ms,
            &turn_id,
        )?,
    )?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &conversation_id,
            replacement.updated_at_ms,
            &turn_id,
        )?,
        encode_updated_index_value(&turn_id, next_revision)?,
    )?;
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, &conversation_id)?;
    Ok((public_turn(document, execution_epoch)?, next_revision))
}

pub(crate) fn visible_sync(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &VisibleSyncRequest,
) -> io::Result<Vec<u8>> {
    if request.messages.is_empty() {
        return encode_response(&Value::Null);
    }
    let Some(attempt) = load_attempt_for_update(database, transaction, &request.attempt_id)? else {
        return encode_response(&Value::Null);
    };
    if attempt.conversation_id != request.conversation_id {
        return encode_response(&Value::Null);
    }
    let Some(root) = load_current_turn_for_attempt(
        database,
        transaction,
        &request.attempt_id,
        &attempt.document,
        &request.conversation_id,
    )?
    else {
        return encode_response(&Value::Null);
    };
    if root.document.get("turnId").and_then(Value::as_str) != Some(request.root_turn_id.as_str()) {
        return encode_response(&Value::Null);
    }
    let root_base_revision = root
        .document
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("visible root revision is malformed"))?;
    let root_projection_before = root
        .document
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let run_id = root
        .document
        .get("runId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or({
            if request.run_id.is_empty() {
                request.attempt_id.as_str()
            } else {
                request.run_id.as_str()
            }
        })
        .to_owned();
    let lane_id = root
        .document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("visible root lane identity is malformed"))?
        .to_owned();
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let mut previous_id = root
        .document
        .get("parentTurnId")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let mut visible_ids = Vec::with_capacity(request.messages.len());
    let mut related = Vec::with_capacity(request.messages.len());
    let mut changed = false;
    let mut root_after = root_projection_before.clone();
    let mut root_public = public_turn(root.document.clone(), execution_epoch)?;
    let mut root_revision = root_base_revision;
    for (index, message) in request.messages.iter().enumerate() {
        let (actor, kind, projection) =
            visible_shape(message, &request.default_kind, &root_projection_before);
        if index == 0 {
            visible_ids.push(request.root_turn_id.clone());
            previous_id = Some(request.root_turn_id.clone());
            let root_kind = root
                .document
                .get("kind")
                .and_then(Value::as_str)
                .unwrap_or("");
            if !visible_flow_kind(root_kind) {
                (root_public, root_revision) = replace_visible_root(
                    database,
                    transaction,
                    &root,
                    VisibleRootReplacement {
                        actor: &actor,
                        kind: &kind,
                        run_id: &run_id,
                        projection: &projection,
                        updated_at_ms: request.updated_at_ms,
                        committed_at_ms: request.committed_at_ms,
                    },
                )?;
                root_after = projection;
                changed = true;
            }
            related.push(root_public.clone());
            continue;
        }
        let turn_id = Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("turn-attempt:{}:visible:{index}", request.attempt_id).as_bytes(),
        )
        .to_string();
        let child_attempt_id = Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!(
                "turn-attempt:{}:visible-attempt:{index}",
                request.attempt_id
            )
            .as_bytes(),
        )
        .to_string();
        visible_ids.push(turn_id.clone());
        let key = turn_key(transaction, &request.conversation_id, &turn_id)?;
        let public = if let Some(stored) = database.entity_get(transaction, &key)? {
            let document = materialize_turn(database, transaction, &stored)?;
            if document.get("turnId").and_then(Value::as_str) != Some(turn_id.as_str())
                || document.get("conversationId").and_then(Value::as_str)
                    != Some(request.conversation_id.as_str())
            {
                return Err(invalid_data("visible child Turn identity differs"));
            }
            public_turn(document, execution_epoch)?
        } else {
            let head_key = entity_key(
                transaction,
                TURN_LANE_HEAD_NAMESPACE,
                &lane_prefix(&request.conversation_id, &lane_id)?,
            )?;
            let ordinal = database
                .entity_get(transaction, &head_key)?
                .map(|value| decode_u64(Some(value), "visible lane head is malformed"))
                .transpose()?
                .unwrap_or(0)
                .checked_add(1)
                .ok_or_else(|| invalid_data("visible lane ordinal overflows"))?;
            let count_key = lane_count_key(transaction, &request.conversation_id, &lane_id)?;
            let count = decode_u64(
                database.entity_get(transaction, &count_key)?,
                "visible lane count is malformed",
            )?
            .checked_add(1)
            .ok_or_else(|| invalid_data("visible lane count overflows"))?;
            let settlement = json!({
                "outcome": "completed", "cause": "phase_completed",
                "providerFinishReason": null, "error": null,
                "resumeOptions": [{"operation":"regenerate", "anchor":{"type":"turn_start"}}]
            });
            let document = Map::from_iter([
                ("turnId".to_owned(), json!(turn_id)),
                ("presentationId".to_owned(), json!(turn_id)),
                ("conversationId".to_owned(), json!(request.conversation_id)),
                ("laneId".to_owned(), json!(lane_id)),
                (
                    "parentTurnId".to_owned(),
                    previous_id
                        .clone()
                        .map(Value::String)
                        .unwrap_or(Value::Null),
                ),
                ("ordinal".to_owned(), json!(ordinal)),
                ("actor".to_owned(), json!(actor)),
                ("kind".to_owned(), json!(kind)),
                ("runId".to_owned(), json!(run_id)),
                ("status".to_owned(), json!("completed")),
                ("currentAttemptId".to_owned(), json!(child_attempt_id)),
                ("projection".to_owned(), Value::Object(projection.clone())),
                ("projectionRevision".to_owned(), json!(1)),
                ("settlement".to_owned(), settlement.clone()),
                ("createdAt".to_owned(), json!(request.updated_at_ms)),
                ("updatedAt".to_owned(), json!(request.updated_at_ms)),
                ("_executionEpoch".to_owned(), json!(execution_epoch)),
            ]);
            let public =
                stage_created_turn(database, transaction, &document, request.committed_at_ms)?;
            database.entity_put(transaction, head_key, ordinal.to_le_bytes().to_vec())?;
            database.entity_put(transaction, count_key, count.to_le_bytes().to_vec())?;
            let child_attempt = Map::from_iter([
                ("attemptId".to_owned(), json!(child_attempt_id)),
                ("conversationId".to_owned(), json!(request.conversation_id)),
                ("turnId".to_owned(), json!(turn_id)),
                (
                    "commandId".to_owned(),
                    json!(format!("run:{}:visible:{index}", request.attempt_id)),
                ),
                ("taskId".to_owned(), json!("")),
                ("operation".to_owned(), json!("generate")),
                ("status".to_owned(), json!("completed")),
                ("baseProjectionRevision".to_owned(), json!(0)),
                ("resumeAnchor".to_owned(), json!({})),
                ("createdAt".to_owned(), json!(request.updated_at_ms)),
                ("startedAt".to_owned(), json!(request.updated_at_ms)),
                ("settledAt".to_owned(), json!(request.updated_at_ms)),
                ("_dispatchMode".to_owned(), json!("")),
                ("_queueId".to_owned(), json!("")),
                ("_queueState".to_owned(), json!("")),
                ("_config".to_owned(), json!({"runId":run_id})),
                ("_error".to_owned(), json!({})),
            ]);
            stage_created_attempt(
                database,
                transaction,
                &child_attempt,
                serde_json::to_vec(&projection)
                    .map_err(|_| invalid_data("visible projection cannot be encoded"))?
                    .len(),
                request.committed_at_ms,
            )?;
            append_attempt_event(
                database,
                transaction,
                AttemptEventAppend {
                    conversation_id: &request.conversation_id,
                    turn_id: &turn_id,
                    attempt_id: &child_attempt_id,
                    projection_revision: 1,
                    event_type: "terminal_settlement",
                    payload: json!({"status":"completed", "settlement":settlement}),
                    occurred_at_ms: request.updated_at_ms,
                    publish_conversation_sync: false,
                },
            )?;
            changed = true;
            public
        };
        related.push(public);
        previous_id = Some(turn_id);
    }
    if changed {
        if root_revision == root_base_revision {
            let (turn_id, projection, _, revision) = update_attempt_turn_lifecycle(
                database,
                transaction,
                &root,
                &request.conversation_id,
                None,
                request.updated_at_ms,
                request.committed_at_ms,
            )?;
            root_revision = revision;
            root_after = projection;
            root_public["projectionRevision"] = json!(revision);
            root_public["updatedAt"] = json!(request.updated_at_ms);
            related[0] = root_public;
            debug_assert_eq!(turn_id, request.root_turn_id);
        }
        let related_turns: Vec<Value> = related
            .into_iter()
            .filter(|turn| {
                turn.get("turnId").and_then(Value::as_str) != Some(request.root_turn_id.as_str())
            })
            .collect();
        let payload = json!({
            "projectionPatch": projection_patch(
                &root_projection_before,
                &root_after,
                root_base_revision,
            )?,
            "turns": related_turns,
            "updateKind": "visible_turns_committed"
        });
        if serde_json::to_vec(&payload)
            .map_err(|_| invalid_data("visible sync event cannot be encoded"))?
            .len()
            > MAX_TRANSACTION_IR_LITERAL_BYTES
        {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "visible sync event exceeds 8 MiB",
            ));
        }
        append_attempt_event(
            database,
            transaction,
            AttemptEventAppend {
                conversation_id: &request.conversation_id,
                turn_id: &request.root_turn_id,
                attempt_id: &request.attempt_id,
                projection_revision: root_revision,
                event_type: "projection_updated",
                payload,
                occurred_at_ms: request.updated_at_ms,
                publish_conversation_sync: true,
            },
        )?;
        crate::search_dirty::mark(
            database,
            transaction,
            TURN_SEARCH_DIRTY_NAMESPACE,
            &request.conversation_id,
        )?;
        crate::conversation_header::advance_for_turn(
            database,
            transaction,
            &request.conversation_id,
            request.updated_at_ms,
            request.committed_at_ms,
            false,
        )?;
    }
    encode_response(&json!({"visibleTurnIds": visible_ids}))
}

pub(crate) fn related_announce(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RelatedAnnounceRequest,
) -> io::Result<Vec<u8>> {
    if request.turn_ids.is_empty() {
        return encode_response(&Value::Bool(false));
    }
    let Some(attempt) = load_attempt_for_update(database, transaction, &request.attempt_id)? else {
        return encode_response(&Value::Bool(false));
    };
    if !matches!(
        attempt.document.get("status").and_then(Value::as_str),
        Some("pending" | "running")
    ) {
        return encode_response(&Value::Bool(false));
    }
    let Some(root) = load_current_turn_for_attempt(
        database,
        transaction,
        &request.attempt_id,
        &attempt.document,
        &attempt.conversation_id,
    )?
    else {
        return encode_response(&Value::Bool(false));
    };
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &attempt.conversation_id,
    )?;
    let root_turn_id = root
        .document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("related root Turn identity is malformed"))?
        .to_owned();
    let mut related_turns = Vec::new();
    let mut related_attempts = Vec::new();
    let mut materialized_bytes = 0_usize;
    for turn_id in &request.turn_ids {
        let Some(stored) = database.entity_get(
            transaction,
            &turn_key(transaction, &attempt.conversation_id, turn_id)?,
        )?
        else {
            continue;
        };
        let document = materialize_turn(database, transaction, &stored)?;
        if document.get("conversationId").and_then(Value::as_str)
            != Some(attempt.conversation_id.as_str())
            || document.get("turnId").and_then(Value::as_str) != Some(turn_id.as_str())
        {
            return Err(invalid_data("related Turn identity differs"));
        }
        let public = public_turn(document.clone(), execution_epoch)?;
        materialized_bytes = materialized_bytes
            .checked_add(
                serde_json::to_vec(&public)
                    .map_err(|_| invalid_data("related Turn cannot be encoded"))?
                    .len(),
            )
            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "related Turn projection exceeds 8 MiB",
                )
            })?;
        if turn_id != &root_turn_id {
            related_turns.push(public);
        }
        let Some(child_attempt_id) = document
            .get("currentAttemptId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        else {
            continue;
        };
        let Some(child) = load_attempt_for_update(database, transaction, child_attempt_id)? else {
            continue;
        };
        if child.conversation_id == attempt.conversation_id {
            related_attempts.push(public_attempt(&child.document)?);
        }
    }
    if related_turns.is_empty() && related_attempts.is_empty() {
        return encode_response(&Value::Bool(false));
    }
    let (turn_id, projection, previous_revision, next_revision) = update_attempt_turn_lifecycle(
        database,
        transaction,
        &root,
        &attempt.conversation_id,
        None,
        request.updated_at_ms,
        request.committed_at_ms,
    )?;
    let bridge_patch = projection_patch(&projection, &projection, previous_revision)?
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("Turn projection bridge is malformed"))?;
    append_attempt_event(
        database,
        transaction,
        AttemptEventAppend {
            conversation_id: &attempt.conversation_id,
            turn_id: &turn_id,
            attempt_id: &request.attempt_id,
            projection_revision: next_revision,
            event_type: "projection_updated",
            payload: json!({
                "projectionPatch": bridge_patch,
                "turns": related_turns,
                "attempts": related_attempts,
                "updateKind": "related_turns_created"
            }),
            occurred_at_ms: request.updated_at_ms,
            publish_conversation_sync: true,
        },
    )?;
    crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &attempt.conversation_id,
        request.updated_at_ms,
        request.committed_at_ms,
        false,
    )?;
    encode_response(&json!({"changed": true}))
}

fn stage_projection_update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ProjectionUpdateRequest,
    mode: ProjectionUpdateMode,
) -> io::Result<StagedProjectionUpdate> {
    let document_key = turn_key(transaction, &request.conversation_id, &request.turn_id)?;
    let stored = database
        .entity_get(transaction, &document_key)?
        .ok_or_else(|| not_found("turn not found"))?;
    let physical_version = crate::versioned_document::stored_document_version(
        &stored,
        DOCUMENT_IDENTITY,
        &turn_logical_key(&request.conversation_id, &request.turn_id),
    )?;
    let (_, document_json) = crate::versioned_document::materialize_stored_document(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        DOCUMENT_IDENTITY,
    )?;
    let mut document = decode_turn_value(&document_json)?;
    let projection_head = projection_head_from_document(&document)?;
    if let Some(head) = &projection_head {
        materialize_projection_head(database, transaction, &mut document, head)?;
    }
    if mode != ProjectionUpdateMode::FencedLiveSteer
        && matches!(
            document.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        )
    {
        return Err(if mode == ProjectionUpdateMode::TypedSettledEdit {
            typed_conflict(TurnConflictKind::InProgress)
        } else {
            conflict("running parent turn cannot change branches")
        });
    }
    let current_projection_revision = document
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    if current_projection_revision != request.expected_projection_revision {
        return Err(if mode == ProjectionUpdateMode::TypedSettledEdit {
            typed_conflict(TurnConflictKind::ProjectionStale)
        } else {
            conflict("parent turn changed before branch operation")
        });
    }
    let next_projection_revision = current_projection_revision
        .checked_add(1)
        .ok_or_else(|| invalid_data("turn projection revision overflow"))?;
    let previous_projection = document
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let next_projection = serde_json::from_slice::<Value>(&request.projection_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("turn projection must be an object"))?;
    let lane_id = document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn lane identity is malformed"))?
        .to_owned();
    let ordinal = document
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
    let previous_updated_at_ms = document
        .get("updatedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn update timestamp is malformed"))?;
    let created_at_ms = document
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn creation timestamp is malformed"))?;
    let activity_timestamp =
        effective_activity_timestamp(next_projection.get("timestamp"), created_at_ms);
    document.insert(
        "projection".to_owned(),
        Value::Object(next_projection.clone()),
    );
    document.insert(
        "projectionRevision".to_owned(),
        Value::from(next_projection_revision),
    );
    document.insert("updatedAt".to_owned(), Value::from(request.updated_at_ms));
    retire_projection_head(
        database,
        transaction,
        &request.conversation_id,
        &request.turn_id,
        projection_head.as_ref(),
    )?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: document_key,
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&request.conversation_id, &request.turn_id),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("turn cannot be encoded"))?,
            expected_version: Some(physical_version),
            updated_at_ms: request.committed_at_ms,
        },
    )?;
    let staged = database
        .entity_get(
            transaction,
            &turn_key(transaction, &request.conversation_id, &request.turn_id)?,
        )?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(transaction, &request.conversation_id, &lane_id, ordinal)?,
        staged,
    )?;
    put_lane_compaction_index(database, transaction, &document)?;
    if lane_id == "main" {
        database.entity_put(
            transaction,
            activity_index_key(transaction, &request.conversation_id, ordinal)?,
            encode_activity_index_value(activity_timestamp),
        )?;
    }
    database.entity_delete(
        transaction,
        updated_index_key(
            transaction,
            &request.conversation_id,
            previous_updated_at_ms,
            &request.turn_id,
        )?,
    )?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &request.conversation_id,
            request.updated_at_ms,
            &request.turn_id,
        )?,
        encode_updated_index_value(&request.turn_id, next_projection_revision)?,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let turn = public_turn(document, execution_epoch)?;
    Ok(StagedProjectionUpdate {
        turn,
        before: previous_projection,
        after: next_projection,
    })
}

fn collect_deletion_closure(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    wanted_turn_ids: &[String],
    wanted_lane_ids: &[String],
    maximum_materialized_bytes: Option<usize>,
) -> io::Result<BTreeMap<String, DeleteTurnRow>> {
    let mut rows = BTreeMap::new();
    let mut pending_turn_ids = wanted_turn_ids.to_vec();
    let mut pending_lane_ids = wanted_lane_ids.to_vec();
    let mut seen_lane_ids = BTreeSet::new();
    let mut materialized_bytes = 0_usize;
    while !pending_turn_ids.is_empty() || !pending_lane_ids.is_empty() {
        while let Some(turn_id) = pending_turn_ids.pop() {
            if rows.contains_key(&turn_id) {
                continue;
            }
            if rows.len() == MAX_DELETE_ROWS {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "turn delete closure exceeds 2000 rows",
                ));
            }
            let document_key = turn_key(transaction, conversation_id, &turn_id)?;
            let stored = database
                .entity_get(transaction, &document_key)?
                .ok_or_else(|| not_found("turn not found"))?;
            let document = materialize_turn(database, transaction, &stored)?;
            if let Some(maximum) = maximum_materialized_bytes {
                materialized_bytes = materialized_bytes
                    .checked_add(
                        serde_json::to_vec(&document)
                            .map_err(|_| invalid_data("Turn deletion row cannot be encoded"))?
                            .len(),
                    )
                    .filter(|bytes| *bytes <= maximum)
                    .ok_or_else(|| {
                        io::Error::new(
                            io::ErrorKind::OutOfMemory,
                            "Turn compaction materialization exceeds 64 MiB",
                        )
                    })?;
            }
            if matches!(
                document.get("status").and_then(Value::as_str),
                Some("pending" | "running")
            ) {
                return Err(conflict("running turn cannot be deleted"));
            }
            if let Some(branch_lanes) = document
                .get("projection")
                .and_then(Value::as_object)
                .and_then(|projection| projection.get("_branchLanes"))
                .and_then(Value::as_array)
            {
                for descriptor in branch_lanes {
                    if let Some(lane_id) = descriptor
                        .as_object()
                        .and_then(|descriptor| descriptor.get("laneId"))
                        .and_then(Value::as_str)
                        .filter(|lane_id| !lane_id.is_empty())
                    {
                        pending_lane_ids.push(lane_id.to_owned());
                    }
                }
            }
            let attempt_id = document
                .get("currentAttemptId")
                .and_then(Value::as_str)
                .map(str::to_owned);
            if let Some(attempt_id) = &attempt_id {
                if let Some(encoded) = attempt_get(database, transaction, attempt_id)? {
                    let attempt: Value = serde_json::from_slice(&encoded)
                        .map_err(|_| invalid_data("attempt response is malformed"))?;
                    if matches!(
                        attempt.get("status").and_then(Value::as_str),
                        Some("pending" | "running")
                    ) {
                        return Err(conflict("running attempt cannot be deleted"));
                    }
                }
            }
            let row = DeleteTurnRow {
                lane_id: document
                    .get("laneId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("turn lane identity is malformed"))?
                    .to_owned(),
                ordinal: document
                    .get("ordinal")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("turn ordinal is malformed"))?,
                updated_at_ms: document
                    .get("updatedAt")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("turn update timestamp is malformed"))?,
                attempt_id,
            };
            rows.insert(turn_id, row);
        }

        let Some(lane_id) = pending_lane_ids.pop() else {
            continue;
        };
        if !seen_lane_ids.insert(lane_id.clone()) {
            continue;
        }
        if seen_lane_ids.len() > MAX_DELETE_BRANCH_LANES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "turn delete closure exceeds 256 branch lanes",
            ));
        }
        let prefix = lane_prefix(conversation_id, &lane_id)?;
        let (mut start, end) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            TURN_LANE_INDEX_NAMESPACE,
            &prefix,
        )?;
        loop {
            let remaining = MAX_DELETE_ROWS + 1 - rows.len().min(MAX_DELETE_ROWS);
            let page_limit = remaining.min(INDEX_PAGE_ROWS);
            let lane_rows = database.entity_scan(transaction, &start, &end, page_limit)?;
            if lane_rows.is_empty() {
                break;
            }
            let row_count = lane_rows.len();
            let continuation = after_key(
                &lane_rows.last().unwrap().0,
                transaction,
                TURN_LANE_INDEX_NAMESPACE,
            )?;
            for (_, stored) in lane_rows {
                let child = materialize_turn(database, transaction, &stored)?;
                if let Some(maximum) = maximum_materialized_bytes {
                    materialized_bytes = materialized_bytes
                        .checked_add(
                            serde_json::to_vec(&child)
                                .map_err(|_| invalid_data("branch Turn cannot be encoded"))?
                                .len(),
                        )
                        .filter(|bytes| *bytes <= maximum)
                        .ok_or_else(|| {
                            io::Error::new(
                                io::ErrorKind::OutOfMemory,
                                "Turn compaction materialization exceeds 64 MiB",
                            )
                        })?;
                }
                let turn_id = child
                    .get("turnId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("branch Turn identity is malformed"))?;
                if !rows.contains_key(turn_id) {
                    pending_turn_ids.push(turn_id.to_owned());
                }
            }
            if rows.len() + pending_turn_ids.len() > MAX_DELETE_ROWS {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "turn delete closure exceeds 2000 rows",
                ));
            }
            if row_count < page_limit {
                break;
            }
            start = continuation;
        }
    }

    Ok(rows)
}

fn delete_owned_identity_claim(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    namespace: &str,
    identity: &str,
) -> io::Result<()> {
    let key = global_identity_claim_key(transaction, namespace, identity)?;
    let expected_owner = transaction.owner_user_id().to_be_bytes();
    match database.entity_get(transaction, &key)? {
        Some(owner)
            if namespace == ATTEMPT_ID_CLAIM_NAMESPACE
                && match decode_attempt_locator(&owner)? {
                    AttemptLocator::LegacyOwner(owner_user_id)
                    | AttemptLocator::Conversation { owner_user_id, .. } => {
                        owner_user_id == transaction.owner_user_id()
                    }
                } =>
        {
            database.entity_delete(transaction, key)
        }
        Some(owner) if owner == expected_owner => database.entity_delete(transaction, key),
        Some(_) => Err(invalid_data("tombstone identity claim owner differs")),
        None => Err(invalid_data("tombstone identity claim is missing")),
    }
}

fn prune_expired_tombstones(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    now_ms: u64,
) -> io::Result<usize> {
    let Some(cutoff_ms) = now_ms.checked_sub(TOMBSTONE_RETENTION_MS) else {
        return Ok(0);
    };
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_TOMBSTONE_AGE_INDEX_NAMESPACE,
        b"",
    )?;
    let end = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_TOMBSTONE_AGE_INDEX_NAMESPACE,
        &cutoff_ms.to_be_bytes(),
    )?;
    let stale = database.entity_scan(transaction, &start, &end, MAX_TOMBSTONE_PRUNE_ROWS)?;
    let pruned = stale.len();
    for (age_key, attempt_id_bytes) in stale {
        let (deleted_at_ms, conversation_id, turn_id) = decode_age_index_identity(&age_key)?;
        if deleted_at_ms >= cutoff_ms {
            return Err(invalid_data("turn tombstone age scan escaped its bound"));
        }
        let attempt_ids = decode_tombstone_attempt_ids(&attempt_id_bytes)?;
        database.entity_delete(transaction, age_key)?;
        database.entity_delete(
            transaction,
            tombstone_index_key(transaction, &conversation_id, deleted_at_ms, &turn_id)?,
        )?;
        delete_owned_identity_claim(database, transaction, TURN_ID_CLAIM_NAMESPACE, &turn_id)?;
        for attempt_id in attempt_ids {
            delete_owned_identity_claim(
                database,
                transaction,
                ATTEMPT_ID_CLAIM_NAMESPACE,
                &attempt_id,
            )?;
        }
    }
    Ok(pruned)
}

fn store_compaction_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    summary_turn_id: &str,
    revision: u64,
    occurred_at_ms: u64,
) -> io::Result<()> {
    let head_key = entity_key(
        transaction,
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )?;
    let sequence = decode_u64(
        database.entity_get(transaction, &head_key)?,
        "conversation sync head is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("conversation sync sequence overflow"))?;
    let event = conversation_sync_event(
        conversation_id,
        sequence,
        "conversation.activity",
        occurred_at_ms,
        json!({"requiresSnapshot": true, "conversationRevision": revision}),
        Some(summary_turn_id),
        None,
    );
    let mut encoded_key = conversation_prefix(conversation_id)?;
    encoded_key.extend_from_slice(&sequence.to_be_bytes());
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded_key)?,
            namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
            logical_key: format!("{conversation_id}\u{1f}{sequence:020}"),
            value_json: serde_json::to_vec(&event)
                .map_err(|_| invalid_data("compaction sync event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: occurred_at_ms,
        },
    )?;
    store_sync_retention_indexes(database, transaction, &event)?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())
}

pub(crate) fn compact(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CompactRequest,
) -> io::Result<Vec<u8>> {
    if request.projection_updates.len() > MAX_TURN_COMPACTION_PROJECTION_UPDATES {
        return Err(invalid_input("too many Turn compaction projection updates"));
    }
    if crate::conversation_header::sync_header(database, transaction, &request.conversation_id)?
        .is_none()
    {
        return Err(not_found("conversation not found"));
    }
    let actual_revision =
        crate::conversation_header::revision(database, transaction, &request.conversation_id)?;
    if actual_revision != request.expected_conversation_revision {
        return encode_response(&json!({
            "applied": false,
            "conversationRevision": actual_revision,
        }));
    }
    let summary_projection = serde_json::from_slice::<Value>(&request.summary_projection_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_input("compaction summary projection must be an object"))?;
    if summary_projection
        .get("compaction")
        .and_then(Value::as_object)
        .and_then(|compaction| compaction.get("blockId"))
        .and_then(Value::as_str)
        != Some("compaction")
    {
        return Err(invalid_input("Turn compaction summary marker is required"));
    }
    let summary_claim = global_identity_claim_key(
        transaction,
        TURN_ID_CLAIM_NAMESPACE,
        &request.summary_turn_id,
    )?;
    if database.entity_get(transaction, &summary_claim)?.is_some() {
        return Err(conflict("summary Turn already exists"));
    }

    let metadata =
        scan_lane_compaction_metadata(database, transaction, &request.conversation_id, "main")?;
    if metadata.is_empty() {
        return Err(not_found("Turn transcript not found"));
    }
    let metadata_by_id = metadata
        .iter()
        .enumerate()
        .map(|(index, row)| (row.turn_id.as_str(), index))
        .collect::<BTreeMap<_, _>>();
    if metadata_by_id.len() != metadata.len() {
        return Err(invalid_data(
            "Turn compaction metadata identity is duplicated",
        ));
    }
    if request
        .delete_turn_ids
        .iter()
        .any(|turn_id| !metadata_by_id.contains_key(turn_id.as_str()))
    {
        return Err(not_found("compaction Turn not found"));
    }
    let rows = collect_deletion_closure(
        database,
        transaction,
        &request.conversation_id,
        &request.delete_turn_ids,
        &[],
        Some(MAX_TURN_COMPACTION_MATERIALIZED_BYTES),
    )?;
    let deleted_ids = rows.keys().cloned().collect::<BTreeSet<_>>();
    let retained = metadata
        .iter()
        .filter(|row| !deleted_ids.contains(&row.turn_id))
        .collect::<Vec<_>>();
    if retained.is_empty() {
        return Err(invalid_input("compaction must preserve a live tail"));
    }
    if retained
        .iter()
        .any(|row| matches!(row.status.as_str(), "pending" | "running"))
    {
        return Err(conflict("a running Turn cannot be compacted"));
    }
    let after_position = request
        .insert_after_turn_id
        .as_ref()
        .map(|turn_id| {
            retained
                .iter()
                .position(|row| row.turn_id == *turn_id)
                .ok_or_else(|| invalid_input("invalid summary predecessor"))
        })
        .transpose()?;
    let before_position = request
        .insert_before_turn_id
        .as_ref()
        .map(|turn_id| {
            retained
                .iter()
                .position(|row| row.turn_id == *turn_id)
                .ok_or_else(|| invalid_input("invalid summary successor"))
        })
        .transpose()?;
    let after_boundary = after_position.map_or(0, |position| position + 1);
    let before_boundary = before_position.unwrap_or(retained.len());
    if before_boundary != after_boundary {
        return Err(invalid_input(
            "summary anchors must be adjacent after folded Turns are removed",
        ));
    }
    let anchor_positions = [after_position, before_position]
        .into_iter()
        .flatten()
        .collect::<BTreeSet<_>>();
    for position in anchor_positions {
        validate_lane_compaction_metadata_target(
            database,
            transaction,
            &request.conversation_id,
            "main",
            retained[position],
        )?;
    }
    let summary_ordinal = match (after_position, before_position) {
        (Some(position), _) => retained[position]
            .ordinal
            .checked_add(1)
            .ok_or_else(|| conflict("no ordinal gap for compaction summary"))?,
        (None, Some(position)) => retained[position]
            .ordinal
            .checked_sub(1)
            .ok_or_else(|| conflict("no ordinal gap for compaction summary"))?,
        (None, None) => return Err(invalid_input("summary insertion anchor is required")),
    };
    if before_position.is_some_and(|position| summary_ordinal >= retained[position].ordinal) {
        return Err(conflict("no ordinal gap for compaction summary"));
    }

    let mut seen_updates = BTreeSet::new();
    let mut projection_bytes = request.summary_projection_json.len();
    for update in &request.projection_updates {
        let Some(row) = retained.iter().find(|row| row.turn_id == update.turn_id) else {
            return Err(invalid_input("invalid retained Turn update"));
        };
        if !seen_updates.insert(update.turn_id.as_str()) {
            return Err(invalid_input("invalid retained Turn update"));
        }
        if row.projection_revision != update.expected_projection_revision {
            return Err(conflict("a retained Turn changed before compaction"));
        }
        projection_bytes = projection_bytes
            .checked_add(update.projection_json.len())
            .filter(|bytes| *bytes <= MAX_TURN_COMPACTION_PROJECTION_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "Turn compaction projections exceed 8 MiB",
                )
            })?;
    }

    let reparented = retained
        .iter()
        .filter(|row| {
            request.insert_before_turn_id.as_deref() == Some(row.turn_id.as_str())
                || row
                    .parent_turn_id
                    .as_ref()
                    .is_some_and(|parent| deleted_ids.contains(parent))
        })
        .copied()
        .collect::<Vec<_>>();
    if reparented.len() > MAX_TURN_COMPACTION_REPARENTED_TURNS
        || rows
            .len()
            .checked_add(reparented.len())
            .and_then(|count| count.checked_add(request.projection_updates.len()))
            .is_none_or(|count| count > MAX_DELETE_ROWS)
    {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "Turn compaction mutation set exceeds its bound",
        ));
    }

    let (deleted_turn_ids, deleted_main_turns) = apply_deletion_rows(
        database,
        transaction,
        &request.conversation_id,
        &rows,
        request.now_ms,
    )?;
    let mut reparented_bytes = 0_usize;
    for row in reparented {
        let key = turn_key(transaction, &request.conversation_id, &row.turn_id)?;
        let stored = database
            .entity_get(transaction, &key)?
            .ok_or_else(|| invalid_data("retained Turn disappeared"))?;
        let physical_version = crate::versioned_document::stored_document_version(
            &stored,
            DOCUMENT_IDENTITY,
            &turn_logical_key(&request.conversation_id, &row.turn_id),
        )?;
        let mut document = materialize_turn(database, transaction, &stored)?;
        if document.get("parentTurnId").and_then(Value::as_str) != row.parent_turn_id.as_deref()
            || document.get("projectionRevision").and_then(Value::as_u64)
                != Some(row.projection_revision)
        {
            return Err(invalid_data("Turn compaction metadata target differs"));
        }
        document.insert(
            "parentTurnId".to_owned(),
            Value::String(request.summary_turn_id.clone()),
        );
        document.insert("updatedAt".to_owned(), Value::from(request.now_ms));
        let document_json = serde_json::to_vec(&Value::Object(document.clone()))
            .map_err(|_| invalid_data("retained Turn cannot be encoded"))?;
        reparented_bytes = reparented_bytes
            .checked_add(document_json.len())
            .filter(|bytes| *bytes <= MAX_TURN_COMPACTION_MATERIALIZED_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "Turn compaction reparenting exceeds 64 MiB",
                )
            })?;
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: key.clone(),
                namespace: DOCUMENT_IDENTITY.to_owned(),
                logical_key: turn_logical_key(&request.conversation_id, &row.turn_id),
                value_json: document_json,
                expected_version: Some(physical_version),
                updated_at_ms: request.now_ms,
            },
        )?;
        let staged = database
            .entity_get(transaction, &key)?
            .ok_or_else(|| invalid_data("staged retained Turn disappeared"))?;
        database.entity_put(
            transaction,
            lane_index_key(transaction, &request.conversation_id, "main", row.ordinal)?,
            staged,
        )?;
        put_lane_compaction_index(database, transaction, &document)?;
        database.entity_delete(
            transaction,
            updated_index_key(
                transaction,
                &request.conversation_id,
                row.updated_at_ms,
                &row.turn_id,
            )?,
        )?;
        database.entity_put(
            transaction,
            updated_index_key(
                transaction,
                &request.conversation_id,
                request.now_ms,
                &row.turn_id,
            )?,
            encode_updated_index_value(&row.turn_id, row.projection_revision)?,
        )?;
    }
    for update in &request.projection_updates {
        stage_projection_update(
            database,
            transaction,
            &ProjectionUpdateRequest {
                conversation_id: request.conversation_id.clone(),
                turn_id: update.turn_id.clone(),
                projection_json: update.projection_json.clone(),
                expected_projection_revision: update.expected_projection_revision,
                updated_at_ms: request.now_ms,
                committed_at_ms: request.now_ms,
            },
            ProjectionUpdateMode::InternalSettledMutation,
        )?;
    }

    if database
        .entity_get(
            transaction,
            &lane_compaction_index_key(
                transaction,
                &request.conversation_id,
                "main",
                summary_ordinal,
            )?,
        )?
        .is_some()
    {
        return Err(conflict("no ordinal gap for compaction summary"));
    }
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let settlement = json!({
        "outcome": "completed",
        "cause": "manual_compaction",
        "resumeOptions": [],
    });
    let summary_document = json!({
        "turnId": request.summary_turn_id,
        "presentationId": request.summary_turn_id,
        "conversationId": request.conversation_id,
        "laneId": "main",
        "parentTurnId": request.insert_after_turn_id,
        "ordinal": summary_ordinal,
        "actor": "assistant",
        "kind": "compaction",
        "runId": "",
        "status": "completed",
        "currentAttemptId": null,
        "projection": summary_projection,
        "projectionRevision": 1,
        "settlement": settlement,
        "createdAt": request.now_ms,
        "updatedAt": request.now_ms,
        "_executionEpoch": execution_epoch,
    });
    let summary_key = turn_key(
        transaction,
        &request.conversation_id,
        &request.summary_turn_id,
    )?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: summary_key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&request.conversation_id, &request.summary_turn_id),
            value_json: serde_json::to_vec(&summary_document)
                .map_err(|_| invalid_data("summary Turn cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: request.now_ms,
        },
    )?;
    database.entity_put(
        transaction,
        summary_claim,
        transaction.owner_user_id().to_be_bytes().to_vec(),
    )?;
    let staged = database
        .entity_get(transaction, &summary_key)?
        .ok_or_else(|| invalid_data("staged summary Turn disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(
            transaction,
            &request.conversation_id,
            "main",
            summary_ordinal,
        )?,
        staged,
    )?;
    put_lane_compaction_index(
        database,
        transaction,
        summary_document
            .as_object()
            .ok_or_else(|| invalid_data("summary Turn cannot be indexed"))?,
    )?;
    database.entity_put(
        transaction,
        activity_index_key(transaction, &request.conversation_id, summary_ordinal)?,
        encode_activity_index_value(effective_activity_timestamp(
            summary_document
                .get("projection")
                .and_then(Value::as_object)
                .and_then(|p| p.get("timestamp")),
            request.now_ms,
        )),
    )?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &request.conversation_id,
            request.now_ms,
            &request.summary_turn_id,
        )?,
        encode_updated_index_value(&request.summary_turn_id, 1)?,
    )?;
    let count_key = lane_count_key(transaction, &request.conversation_id, "main")?;
    let count = decode_u64(
        database.entity_get(transaction, &count_key)?,
        "Turn lane count is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("Turn lane count overflow"))?;
    database.entity_put(transaction, count_key, count.to_le_bytes().to_vec())?;
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    let revision = if deleted_main_turns == 0 {
        crate::conversation_header::advance_for_turn(
            database,
            transaction,
            &request.conversation_id,
            request.now_ms,
            request.now_ms,
            true,
        )?
    } else {
        crate::conversation_header::advance_for_turn_delete(
            database,
            transaction,
            &request.conversation_id,
            deleted_main_turns - 1,
            request.now_ms,
            request.now_ms,
        )?
    };
    if revision != request.expected_conversation_revision + 1 {
        return Err(invalid_data("Turn compaction revision differs"));
    }
    store_compaction_sync_event(
        database,
        transaction,
        &request.conversation_id,
        &request.summary_turn_id,
        revision,
        request.now_ms,
    )?;
    let turn = public_turn(
        summary_document
            .as_object()
            .cloned()
            .ok_or_else(|| invalid_data("summary Turn is malformed"))?,
        execution_epoch,
    )?;
    encode_response(&json!({
        "applied": true,
        "turn": turn,
        "deletedTurnIds": deleted_turn_ids,
        "conversationRevision": revision,
    }))
}

pub(crate) fn delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    wanted_turn_ids: &[String],
    deleted_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let rows = collect_deletion_closure(
        database,
        transaction,
        conversation_id,
        wanted_turn_ids,
        &[],
        None,
    )?;
    let (deleted_turn_ids, deleted_main_turns) =
        apply_deletion_rows(database, transaction, conversation_id, &rows, deleted_at_ms)?;
    let revision = crate::conversation_header::advance_for_turn_delete(
        database,
        transaction,
        conversation_id,
        deleted_main_turns,
        deleted_at_ms,
        deleted_at_ms,
    )?;
    store_delete_sync_event(
        database,
        transaction,
        conversation_id,
        &deleted_turn_ids,
        &[],
        revision,
        deleted_at_ms,
    )?;
    encode_response(&json!({
        "deletedTurnIds": deleted_turn_ids,
        "conversationRevision": revision
    }))
}

fn apply_deletion_rows(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    rows: &BTreeMap<String, DeleteTurnRow>,
    deleted_at_ms: u64,
) -> io::Result<(Vec<String>, u64)> {
    let missing_attempt_documents_are_inert =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)? > 0;
    let mut attempts_by_turn = BTreeMap::<String, Vec<Map<String, Value>>>::new();
    for (turn_id, row) in rows {
        let count = match database.entity_get(
            transaction,
            &attempt_turn_count_key(transaction, conversation_id, turn_id)?,
        )? {
            None if row.attempt_id.is_some() => 1,
            stored => decode_u64(stored, "turn attempt count is malformed")?,
        };
        let attempts = load_attempt_turn_directory(
            database,
            transaction,
            conversation_id,
            turn_id,
            row.attempt_id.as_deref(),
        )?;
        if attempts.len() as u64 != count
            || row.attempt_id.as_ref().is_some_and(|current| {
                !attempts.iter().any(|entry| {
                    directory_entry_text(entry, "attemptId").ok() == Some(current.as_str())
                })
            })
        {
            return Err(invalid_data("turn attempt directory count is inconsistent"));
        }
        attempts_by_turn.insert(turn_id.clone(), attempts);
    }
    prune_expired_tombstones(database, transaction, deleted_at_ms)?;
    let deleted_turn_ids = rows.keys().cloned().collect::<Vec<_>>();
    let mut deleted_by_lane = BTreeMap::<String, u64>::new();
    let mut deleted_main_turns = 0_u64;
    for (turn_id, row) in rows {
        *deleted_by_lane.entry(row.lane_id.clone()).or_default() += 1;
        deleted_main_turns += u64::from(row.lane_id == "main");

        let document_key = turn_key(transaction, conversation_id, turn_id)?;
        let lane_key = lane_index_key(transaction, conversation_id, &row.lane_id, row.ordinal)?;
        let updated_key =
            updated_index_key(transaction, conversation_id, row.updated_at_ms, turn_id)?;
        let tombstone_key =
            tombstone_index_key(transaction, conversation_id, deleted_at_ms, turn_id)?;
        let tombstone_age_key =
            tombstone_age_index_key(transaction, deleted_at_ms, conversation_id, turn_id)?;
        database.entity_delete(transaction, document_key)?;
        database.entity_delete(transaction, lane_key)?;
        database.entity_delete(
            transaction,
            lane_compaction_index_key(transaction, conversation_id, &row.lane_id, row.ordinal)?,
        )?;
        if row.lane_id == "main" {
            database.entity_delete(
                transaction,
                activity_index_key(transaction, conversation_id, row.ordinal)?,
            )?;
        }
        database.entity_delete(transaction, updated_key)?;
        database.entity_delete(
            transaction,
            attempt_turn_count_key(transaction, conversation_id, turn_id)?,
        )?;
        database.entity_delete(
            transaction,
            attempt_turn_directory_key(transaction, conversation_id, turn_id)?,
        )?;
        let attempts = attempts_by_turn
            .get(turn_id)
            .ok_or_else(|| invalid_data("turn attempt directory preflight is missing"))?;
        for entry in attempts {
            let attempt_id = directory_entry_text(entry, "attemptId")?;
            let clustered = attempt_key(transaction, conversation_id, attempt_id)?;
            let legacy = legacy_attempt_key(transaction, attempt_id)?;
            let clustered_stored = database.entity_get(transaction, &clustered)?;
            let legacy_stored = database.entity_get(transaction, &legacy)?;
            let clustered_present = clustered_stored.is_some();
            let legacy_present = legacy_stored.is_some();
            if clustered_present && legacy_present
                || !clustered_present && !legacy_present && !missing_attempt_documents_are_inert
            {
                return Err(invalid_data(
                    "turn attempt storage is missing or has duplicate layouts",
                ));
            }
            remove_directory_entry_indexes(database, transaction, conversation_id, entry)?;
            if clustered_present || legacy_present {
                database.entity_delete(
                    transaction,
                    if clustered_present { clustered } else { legacy },
                )?;
            }
            database.entity_delete(
                transaction,
                attempt_event_head_key(transaction, attempt_id)?,
            )?;
            let event_prefix = attempt_event_prefix(attempt_id)?;
            let (event_start, event_end) = EntityKey::prefix_range(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                ATTEMPT_EVENT_NAMESPACE,
                &event_prefix,
            )?;
            database.entity_retire_range(transaction, &event_start, &event_end)?;
        }
        let tombstone_attempt_ids = attempts
            .iter()
            .map(|entry| directory_entry_text(entry, "attemptId").map(str::to_owned))
            .collect::<io::Result<Vec<_>>>()?;
        let tombstone_value = encode_tombstone_attempt_ids(&tombstone_attempt_ids)?;
        database.entity_put(transaction, tombstone_key, tombstone_value.clone())?;
        database.entity_put(transaction, tombstone_age_key, tombstone_value)?;
    }

    for (lane_id, deleted_count) in deleted_by_lane {
        let count_key = lane_count_key(transaction, conversation_id, &lane_id)?;
        let count = decode_u64(
            database.entity_get(transaction, &count_key)?,
            "turn lane count is malformed",
        )?;
        let remaining = count
            .checked_sub(deleted_count)
            .ok_or_else(|| invalid_data("turn lane count underflow"))?;
        if remaining == 0 {
            database.entity_delete(transaction, count_key)?;
        } else {
            database.entity_put(transaction, count_key, remaining.to_le_bytes().to_vec())?;
        }
    }
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        conversation_id,
    )?;
    Ok((deleted_turn_ids, deleted_main_turns))
}

pub(crate) fn branch_create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &BranchCreateRequest,
) -> io::Result<Vec<u8>> {
    let document_key = turn_key(
        transaction,
        &request.conversation_id,
        &request.parent_turn_id,
    )?;
    let stored = database
        .entity_get(transaction, &document_key)?
        .ok_or_else(|| not_found("parent turn not found"))?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let parent = public_turn(
        materialize_turn(database, transaction, &stored)?,
        execution_epoch,
    )?;
    if matches!(
        parent.get("status").and_then(Value::as_str),
        Some("pending" | "running")
    ) {
        return Err(conflict("running parent turn cannot be branched"));
    }
    let current_revision = parent
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("parent projection revision is malformed"))?;
    if current_revision != request.expected_projection_revision {
        return Err(conflict("parent turn changed before branch creation"));
    }
    let mut projection = parent
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("parent projection is malformed"))?;
    let lane = json!({
        "laneId": request.lane_id,
        "parentTurnId": request.parent_turn_id,
        "title": request.title,
        "icon": "⑂",
        "kind": request.kind,
        "anchorText": request.anchor_text,
        "parentSelection": request.parent_selection,
        "createdAt": request.updated_at_ms
    });
    let descriptors = projection
        .entry("_branchLanes".to_owned())
        .or_insert_with(|| Value::Array(Vec::new()))
        .as_array_mut()
        .ok_or_else(|| invalid_data("parent branch descriptors are malformed"))?;
    descriptors.push(lane.clone());
    let update = ProjectionUpdateRequest {
        conversation_id: request.conversation_id.clone(),
        turn_id: request.parent_turn_id.clone(),
        projection_json: serde_json::to_vec(&projection)
            .map_err(|_| invalid_data("branch projection cannot be encoded"))?,
        expected_projection_revision: current_revision,
        updated_at_ms: request.updated_at_ms,
        committed_at_ms: request.committed_at_ms,
    };
    let staged = stage_projection_update(
        database,
        transaction,
        &update,
        ProjectionUpdateMode::InternalSettledMutation,
    )?;
    let conversation_revision = crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &request.conversation_id,
        request.updated_at_ms,
        request.committed_at_ms,
        false,
    )?;
    store_projection_sync_event(
        database,
        transaction,
        &update,
        &staged.before,
        &staged.after,
        conversation_revision,
        &[],
    )?;
    encode_response(&json!({
        "turn": staged.turn,
        "lane": lane,
        "conversationRevision": conversation_revision
    }))
}

pub(crate) fn branch_delete(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &BranchDeleteRequest,
) -> io::Result<Vec<u8>> {
    let document_key = turn_key(
        transaction,
        &request.conversation_id,
        &request.parent_turn_id,
    )?;
    let stored = database
        .entity_get(transaction, &document_key)?
        .ok_or_else(|| not_found("parent turn not found"))?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let parent = public_turn(
        materialize_turn(database, transaction, &stored)?,
        execution_epoch,
    )?;
    if matches!(
        parent.get("status").and_then(Value::as_str),
        Some("pending" | "running")
    ) {
        return Err(conflict("running parent turn cannot delete a branch"));
    }
    if let Some(attempt_id) = parent.get("currentAttemptId").and_then(Value::as_str) {
        if let Some(encoded) = attempt_get(database, transaction, attempt_id)? {
            let attempt: Value = serde_json::from_slice(&encoded)
                .map_err(|_| invalid_data("parent attempt response is malformed"))?;
            if matches!(
                attempt.get("status").and_then(Value::as_str),
                Some("pending" | "running")
            ) {
                return Err(conflict("running parent turn cannot delete a branch"));
            }
        }
    }
    let projection_revision = parent
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("parent projection revision is malformed"))?;
    let mut projection = parent
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("parent projection is malformed"))?;
    let descriptors = projection
        .get("_branchLanes")
        .and_then(Value::as_array)
        .ok_or_else(|| not_found("branch lane not found"))?;
    let kept = descriptors
        .iter()
        .filter(|descriptor| {
            descriptor.get("laneId").and_then(Value::as_str) != Some(request.lane_id.as_str())
        })
        .cloned()
        .collect::<Vec<_>>();
    if kept.len() == descriptors.len() {
        return Err(not_found("branch lane not found"));
    }
    projection.insert("_branchLanes".to_owned(), Value::Array(kept));
    let rows = collect_deletion_closure(
        database,
        transaction,
        &request.conversation_id,
        &[],
        std::slice::from_ref(&request.lane_id),
        None,
    )?;
    if rows.contains_key(&request.parent_turn_id) {
        return Err(invalid_data("branch lane contains its parent turn"));
    }
    let (deleted_turn_ids, deleted_main_turns) = apply_deletion_rows(
        database,
        transaction,
        &request.conversation_id,
        &rows,
        request.deleted_at_ms,
    )?;
    let update = ProjectionUpdateRequest {
        conversation_id: request.conversation_id.clone(),
        turn_id: request.parent_turn_id.clone(),
        projection_json: serde_json::to_vec(&projection)
            .map_err(|_| invalid_data("branch projection cannot be encoded"))?,
        expected_projection_revision: projection_revision,
        updated_at_ms: request.deleted_at_ms,
        committed_at_ms: request.committed_at_ms,
    };
    let staged = stage_projection_update(
        database,
        transaction,
        &update,
        ProjectionUpdateMode::InternalSettledMutation,
    )?;
    let conversation_revision = crate::conversation_header::advance_for_turn_delete(
        database,
        transaction,
        &request.conversation_id,
        deleted_main_turns,
        request.deleted_at_ms,
        request.committed_at_ms,
    )?;
    store_projection_sync_event(
        database,
        transaction,
        &update,
        &staged.before,
        &staged.after,
        conversation_revision,
        &deleted_turn_ids,
    )?;
    encode_response(&json!({
        "turn": staged.turn,
        "deletedLaneId": request.lane_id,
        "deletedTurnIds": deleted_turn_ids,
        "conversationRevision": conversation_revision
    }))
}

pub(crate) fn exists(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    let prefix = conversation_prefix(conversation_id)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_DOCUMENT_NAMESPACE,
        &prefix,
    )?;
    encode_response(&Value::Bool(
        !database
            .entity_scan(transaction, &start, &end, 1)?
            .is_empty(),
    ))
}

pub(crate) fn get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let key = turn_key(transaction, conversation_id, turn_id)?;
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    encode_response(&public_turn(
        materialize_turn(database, transaction, &stored)?,
        execution_epoch,
    )?)
    .map(Some)
}

fn supported_legacy_image_media_type(value: &str) -> Option<String> {
    let normalized = value.trim().to_ascii_lowercase();
    matches!(
        normalized.as_str(),
        "image/png" | "image/jpeg" | "image/gif" | "image/webp"
    )
    .then_some(normalized)
}

fn legacy_data_uri(value: &str) -> Option<(String, &str)> {
    let body = value.strip_prefix("data:")?;
    let marker = body.find(";base64,")?;
    let media_type = supported_legacy_image_media_type(&body[..marker])?;
    let encoded = &body[marker + ";base64,".len()..];
    let maximum_encoded_characters =
        crate::generated_tofudb_ir::MAX_LEGACY_TURN_IMAGE_BYTES.div_ceil(3) * 4;
    (!encoded.is_empty()
        && encoded.len() <= maximum_encoded_characters
        && encoded.len().is_multiple_of(4))
    .then_some((media_type, encoded))
}

pub(crate) fn image_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    expected_projection_revision: u64,
    image_index: usize,
) -> io::Result<Option<Vec<u8>>> {
    let key = turn_key(transaction, conversation_id, turn_id)?;
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let mut turn = public_turn(
        materialize_turn(database, transaction, &stored)?,
        execution_epoch,
    )?;
    let turn = turn
        .as_object_mut()
        .ok_or_else(|| invalid_data("public Turn is malformed"))?;
    if !matches!(
        turn.get("status").and_then(Value::as_str),
        Some("completed" | "interrupted" | "truncated" | "failed" | "superseded")
    ) {
        return Ok(None);
    }
    let current_revision = turn
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    if current_revision != expected_projection_revision {
        return encode_response(&json!({
            "stale": true,
            "projectionRevision": current_revision
        }))
        .map(Some);
    }
    let Some(image) = turn
        .get_mut("projection")
        .and_then(Value::as_object_mut)
        .and_then(|projection| projection.get_mut("images"))
        .and_then(Value::as_array_mut)
        .and_then(|images| images.get_mut(image_index))
        .and_then(Value::as_object_mut)
    else {
        return Ok(None);
    };
    let maximum_encoded_characters =
        crate::generated_tofudb_ir::MAX_LEGACY_TURN_IMAGE_BYTES.div_ceil(3) * 4;
    let preview = image
        .get("preview")
        .and_then(Value::as_str)
        .and_then(legacy_data_uri);
    let direct_media_type = image
        .get("mediaType")
        .or_else(|| image.get("mimeType"))
        .and_then(Value::as_str)
        .and_then(supported_legacy_image_media_type);
    let direct_is_valid = image
        .get("base64")
        .and_then(Value::as_str)
        .is_some_and(|encoded| {
            !encoded.is_empty()
                && encoded.len() <= maximum_encoded_characters
                && encoded.len().is_multiple_of(4)
        });
    let (media_type, encoded) = if direct_is_valid {
        let media_type = direct_media_type.or_else(|| preview.as_ref().map(|item| item.0.clone()));
        let Some(media_type) = media_type else {
            return Ok(None);
        };
        let encoded = image
            .remove("base64")
            .and_then(|value| value.as_str().map(str::to_owned))
            .ok_or_else(|| invalid_data("legacy Turn image changed while reading"))?;
        (media_type, encoded)
    } else {
        let Some((media_type, encoded)) = preview else {
            return Ok(None);
        };
        (media_type, encoded.to_owned())
    };
    let encoded = serde_json::to_vec(&json!({
        "stale": false,
        "projectionRevision": current_revision,
        "mediaType": media_type,
        "base64": encoded,
    }))
    .map_err(|_| invalid_data("legacy Turn image response cannot be encoded"))?;
    if encoded.len() > crate::generated_storage_v2::MAX_STREAMED_RESPONSE_PAYLOAD_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "legacy Turn image response exceeds the streaming bound",
        ));
    }
    Ok(Some(encoded))
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: Option<&str>,
) -> io::Result<Vec<u8>> {
    encode_response(&Value::Array(list_values(
        database,
        transaction,
        conversation_id,
        lane_id,
    )?))
}

pub(crate) fn list_delta(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    since_ms: u64,
    known_revisions: &BTreeMap<String, u64>,
    server_now_ms: u64,
) -> io::Result<Vec<u8>> {
    let lower_bound_ms = since_ms.saturating_sub(DELTA_OVERLAP_MS);
    let conversation = conversation_prefix(conversation_id)?;
    let (_, updated_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_UPDATED_INDEX_NAMESPACE,
        &conversation,
    )?;
    let mut updated_start = updated_index_key(transaction, conversation_id, lower_bound_ms, "")?;
    let mut candidates = Vec::new();
    while candidates.len() <= MAX_DELTA_ROWS {
        let page_limit = (MAX_DELTA_ROWS + 1 - candidates.len()).min(INDEX_PAGE_ROWS);
        let rows = database.entity_scan(transaction, &updated_start, &updated_end, page_limit)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().unwrap().0,
            transaction,
            TURN_UPDATED_INDEX_NAMESPACE,
        )?;
        for (_, value) in rows {
            candidates.push(decode_updated_index_value(&value)?);
        }
        if row_count < page_limit {
            break;
        }
        updated_start = continuation;
    }
    if candidates.len() > MAX_DELTA_ROWS {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn delta exceeds 2000 changed rows",
        ));
    }

    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let mut turns = Vec::with_capacity(candidates.len());
    let mut response_bytes = 128_usize;
    for (turn_id, projection_revision) in candidates {
        if known_revisions
            .get(&turn_id)
            .is_some_and(|known| projection_revision <= *known)
        {
            continue;
        }
        let document_key = turn_key(transaction, conversation_id, &turn_id)?;
        let stored = database
            .entity_get(transaction, &document_key)?
            .ok_or_else(|| invalid_data("turn updated index target is missing"))?;
        let turn = public_turn(
            materialize_turn(database, transaction, &stored)?,
            execution_epoch,
        )?;
        if turn.get("projectionRevision").and_then(Value::as_u64) != Some(projection_revision) {
            return Err(invalid_data("turn updated index revision differs"));
        }
        response_bytes = response_bytes
            .checked_add(
                serde_json::to_vec(&turn)
                    .map_err(|_| invalid_data("turn delta cannot be encoded"))?
                    .len()
                    + usize::from(!turns.is_empty()),
            )
            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
            .ok_or_else(|| {
                io::Error::new(io::ErrorKind::OutOfMemory, "turn delta exceeds 8 MiB")
            })?;
        turns.push(turn);
    }
    turns.sort_by_key(|turn| {
        turn.get("ordinal")
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX)
    });

    let (_, tombstone_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_TOMBSTONE_NAMESPACE,
        &conversation,
    )?;
    let mut tombstone_start =
        tombstone_index_key(transaction, conversation_id, lower_bound_ms, "")?;
    let mut deleted_turn_ids = Vec::new();
    while deleted_turn_ids.len() <= MAX_DELTA_ROWS {
        let page_limit = (MAX_DELTA_ROWS + 1 - deleted_turn_ids.len()).min(INDEX_PAGE_ROWS);
        let rows =
            database.entity_scan(transaction, &tombstone_start, &tombstone_end, page_limit)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().unwrap().0,
            transaction,
            TURN_TOMBSTONE_NAMESPACE,
        )?;
        for (key, _) in rows {
            let turn_id = decode_tombstone_turn_id(&key, conversation.len())?;
            response_bytes = response_bytes
                .checked_add(turn_id.len() + 3)
                .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::OutOfMemory, "turn delta exceeds 8 MiB")
                })?;
            deleted_turn_ids.push(turn_id);
        }
        if row_count < page_limit {
            break;
        }
        tombstone_start = continuation;
    }
    if deleted_turn_ids.len() > MAX_DELTA_ROWS {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn delta exceeds 2000 tombstones",
        ));
    }

    encode_response(&json!({
        "turns": turns,
        "deletedTurnIds": deleted_turn_ids,
        "serverNowMs": server_now_ms
    }))
}

pub(crate) fn list_values(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: Option<&str>,
) -> io::Result<Vec<Value>> {
    let (namespace, prefix) = if let Some(lane_id) = lane_id {
        (
            TURN_LANE_INDEX_NAMESPACE,
            lane_prefix(conversation_id, lane_id)?,
        )
    } else {
        (
            TURN_DOCUMENT_NAMESPACE,
            conversation_prefix(conversation_id)?,
        )
    };
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        namespace,
        &prefix,
    )?;
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let mut turns = Vec::new();
    let mut response_bytes = 2_usize;
    for page in 0..MAX_SCAN_PAGES {
        let rows = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(&rows.last().unwrap().0, transaction, namespace)?;
        for (_, stored) in rows {
            if turns.len() == MAX_LIST_ROWS {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "turn list exceeds 10000 rows",
                ));
            }
            let turn = public_turn(
                materialize_turn(database, transaction, &stored)?,
                execution_epoch,
            )?;
            response_bytes = response_bytes
                .checked_add(
                    serde_json::to_vec(&turn)
                        .map_err(|_| invalid_data("turn response cannot be encoded"))?
                        .len()
                        + usize::from(!turns.is_empty()),
                )
                .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                .ok_or_else(|| {
                    io::Error::new(io::ErrorKind::OutOfMemory, "turn list exceeds 8 MiB")
                })?;
            turns.push(turn);
        }
        if row_count < INDEX_PAGE_ROWS {
            break;
        }
        if page + 1 == MAX_SCAN_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "turn list scan exceeds page budget",
            ));
        }
        start = continuation;
    }
    turns.sort_by_key(|turn| {
        turn.get("ordinal")
            .and_then(Value::as_u64)
            .unwrap_or(u64::MAX)
    });
    Ok(turns)
}

fn legacy_role(actor: &str) -> &'static str {
    if matches!(actor, "human" | "critic" | "virtual_user") {
        "user"
    } else {
        "assistant"
    }
}

fn append_search_content(parts: &mut Vec<String>, content: &Value) -> io::Result<()> {
    match content {
        Value::String(value) if !value.is_empty() => parts.push(value.clone()),
        Value::Array(items) => {
            for item in items {
                match item {
                    Value::String(value) => parts.push(value.clone()),
                    Value::Object(value) => match value.get("text") {
                        None => parts.push(String::new()),
                        Some(Value::String(text)) => parts.push(text.clone()),
                        Some(_) => return Err(invalid_data("turn search text field is malformed")),
                    },
                    _ => {}
                }
            }
        }
        _ => {}
    }
    Ok(())
}

fn bounded_search_text(actor: &str, projection: &Map<String, Value>) -> io::Result<String> {
    // The legacy projector overwrites role from actor before accepting the
    // message, so every valid Turn projection participates in search.
    let _role = legacy_role(actor);
    let mut parts = Vec::new();
    if let Some(content) = projection.get("content") {
        append_search_content(&mut parts, content)?;
    }
    for field in ["thinking", "translatedContent", "originalContent"] {
        if let Some(Value::String(value)) = projection.get(field) {
            if !value.is_empty() {
                parts.push(value.clone());
            }
        }
    }
    let text = parts.join("\n");
    if text.len() <= SEARCH_TEXT_MAX_BYTES {
        return Ok(text);
    }
    let mut end = SEARCH_TEXT_MAX_BYTES;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    Ok(text[..end].to_owned())
}

pub(crate) fn search_projection_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    cursor: &[u8],
) -> io::Result<SearchProjectionPage> {
    let prefix = conversation_prefix(conversation_id)?;
    let (prefix_start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_DOCUMENT_NAMESPACE,
        &prefix,
    )?;
    let start = if cursor.is_empty() {
        prefix_start
    } else {
        if !cursor.starts_with(&prefix) {
            return Err(invalid_input(
                "turn search source cursor escaped conversation",
            ));
        }
        let mut successor = cursor.to_vec();
        successor.push(0);
        entity_key(transaction, TURN_DOCUMENT_NAMESPACE, &successor)?
    };
    let rows = database.entity_scan(transaction, &start, &end, SEARCH_PROJECTION_PAGE_ROWS + 1)?;
    let candidate_count = rows.len().min(SEARCH_PROJECTION_PAGE_ROWS);
    let mut turns = Vec::with_capacity(candidate_count);
    let mut next_cursor = cursor.to_vec();
    let mut skipped_oversized = 0usize;
    let mut source_bytes = 0u64;
    let mut consumed = 0usize;
    for (document_key, stored) in rows.iter().take(candidate_count) {
        let logical_bytes =
            crate::versioned_document::stored_document_logical_bytes(stored, DOCUMENT_IDENTITY)?;
        if logical_bytes > SEARCH_PROJECTION_PAGE_BYTES {
            skipped_oversized += 1;
            next_cursor = document_key.key_bytes().to_vec();
            consumed += 1;
            continue;
        }
        if source_bytes
            .checked_add(logical_bytes)
            .filter(|bytes| *bytes <= SEARCH_PROJECTION_PAGE_BYTES)
            .is_none()
        {
            break;
        }
        let document = materialize_turn(database, transaction, stored)?;
        let turn_id = document
            .get("turnId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("turn search source identity is malformed"))?
            .to_owned();
        if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
            || document_key != &turn_key(transaction, conversation_id, &turn_id)?
        {
            return Err(invalid_data("turn search source identity is inconsistent"));
        }
        let lane_id = document
            .get("laneId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("turn search source lane is malformed"))?;
        let status = document
            .get("status")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("turn search source status is malformed"))?;
        if lane_id == "main" && !matches!(status, "pending" | "running") {
            let actor = document
                .get("actor")
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("turn search source actor is malformed"))?;
            let projection = document
                .get("projection")
                .and_then(Value::as_object)
                .ok_or_else(|| invalid_data("turn search source projection is malformed"))?;
            turns.push(SearchProjectionTurn {
                turn_id,
                ordinal: document
                    .get("ordinal")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("turn search source ordinal is malformed"))?,
                search_text: bounded_search_text(actor, projection)?,
            });
        }
        source_bytes += logical_bytes;
        next_cursor = document_key.key_bytes().to_vec();
        consumed += 1;
    }
    Ok(SearchProjectionPage {
        turns,
        next_cursor,
        complete: consumed == rows.len(),
        skipped_oversized,
        source_bytes,
    })
}

fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn legacy_message(turn: &Value) -> io::Result<Value> {
    let turn = turn
        .as_object()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let mut message = turn
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    message.remove("role");
    let actor = turn
        .get("actor")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn actor is malformed"))?;
    message.insert(
        "role".to_owned(),
        Value::String(legacy_role(actor).to_owned()),
    );
    for (target, source) in [
        ("_turnId", "turnId"),
        ("_attemptId", "currentAttemptId"),
        ("_turnActor", "actor"),
        ("_turnKind", "kind"),
        ("_turnLaneId", "laneId"),
        ("_turnStatus", "status"),
        ("_turnSettlement", "settlement"),
        ("_projectionRevision", "projectionRevision"),
    ] {
        message.insert(
            target.to_owned(),
            turn.get(source).cloned().unwrap_or(Value::Null),
        );
    }
    message.insert("_commandPending".to_owned(), Value::Null);
    if !message.get("timestamp").is_some_and(python_truthy) {
        message.insert(
            "timestamp".to_owned(),
            turn.get("createdAt").cloned().unwrap_or(Value::from(0)),
        );
    }
    Ok(Value::Object(message))
}

pub(crate) fn legacy_messages(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    window: usize,
    before_sequence: Option<i64>,
) -> io::Result<(Vec<Value>, usize, usize, usize)> {
    let turns = list_values(database, transaction, conversation_id, Some("main"))?;
    let total = turns.len();
    let end = before_sequence.map_or(total, |before| {
        usize::try_from(before.max(0))
            .unwrap_or(usize::MAX)
            .min(total)
    });
    let start = if window == 0 {
        0
    } else {
        end.saturating_sub(window)
    };
    let messages = turns[start..end]
        .iter()
        .map(legacy_message)
        .collect::<io::Result<Vec<_>>>()?;
    Ok((messages, total, start, end))
}

fn tolerant_activity_timestamp(value: Option<&Value>) -> i128 {
    match value {
        None | Some(Value::Null) => 0,
        Some(Value::Bool(value)) => i128::from(*value),
        Some(Value::Number(value)) => value
            .as_i64()
            .map(i128::from)
            .or_else(|| value.as_u64().map(i128::from))
            .or_else(|| {
                value
                    .as_f64()
                    .and_then(|number| number.is_finite().then(|| number.trunc() as i128))
            })
            .unwrap_or(0),
        Some(Value::String(value)) => value.trim().parse::<i128>().unwrap_or(0),
        Some(Value::Array(_) | Value::Object(_)) => 0,
    }
}

fn insert_activity_interval(
    intervals: &mut BTreeSet<usize>,
    timestamp: i128,
    fallback_timestamp_ms: u64,
    boundaries_ms: &[i64],
) {
    let timestamp = if timestamp == 0 {
        i128::from(fallback_timestamp_ms)
    } else {
        timestamp
    };
    let upper = boundaries_ms.partition_point(|boundary| i128::from(*boundary) <= timestamp);
    if upper > 0 && upper < boundaries_ms.len() {
        intervals.insert(upper - 1);
    }
}

pub(crate) fn activity_intervals(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    fallback_timestamp_ms: u64,
    boundaries_ms: &[i64],
    remaining_turn_rows: &mut usize,
) -> io::Result<BTreeSet<usize>> {
    let prefix = lane_prefix(conversation_id, "main")?;
    let lane_count = decode_u64(
        database.entity_get(
            transaction,
            &lane_count_key(transaction, conversation_id, "main")?,
        )?,
        "turn lane count is malformed",
    )?;
    if lane_count > MAX_LIST_ROWS as u64 {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "Turn activity scan exceeds 10000 rows per conversation",
        ));
    }
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_ACTIVITY_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut intervals = BTreeSet::new();
    let mut indexed_rows = 0_u64;
    for page in 0..MAX_SCAN_PAGES {
        let rows = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().expect("nonempty Turn activity page").0,
            transaction,
            TURN_ACTIVITY_INDEX_NAMESPACE,
        )?;
        for (_, encoded) in rows {
            if *remaining_turn_rows == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "Turn activity scan exceeds its aggregate row budget",
                ));
            }
            *remaining_turn_rows -= 1;
            indexed_rows += 1;
            insert_activity_interval(
                &mut intervals,
                decode_activity_index_value(&encoded)?,
                fallback_timestamp_ms,
                boundaries_ms,
            );
        }
        if row_count < INDEX_PAGE_ROWS {
            break;
        }
        if page + 1 == MAX_SCAN_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "Turn activity scan exceeds its page budget",
            ));
        }
        start = continuation;
    }
    if indexed_rows == lane_count {
        return Ok(intervals);
    }
    if indexed_rows != 0 {
        return Err(invalid_data(
            "Turn activity timestamp index count is inconsistent",
        ));
    }

    // Authorities created before the compact index was introduced have no
    // partial rows. Read each legacy Turn once without retaining projections;
    // all new writes maintain the scalar index transactionally.
    let (mut start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    let mut legacy_rows = 0_u64;
    for page in 0..MAX_SCAN_PAGES {
        let rows = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().expect("nonempty legacy Turn activity page").0,
            transaction,
            TURN_LANE_INDEX_NAMESPACE,
        )?;
        for (_, stored) in rows {
            if *remaining_turn_rows == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "Turn activity scan exceeds its aggregate row budget",
                ));
            }
            *remaining_turn_rows -= 1;
            legacy_rows += 1;
            let turn = materialize_turn(database, transaction, &stored)?;
            if turn.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
                || turn.get("laneId").and_then(Value::as_str) != Some("main")
            {
                return Err(invalid_data("Turn activity index identity is inconsistent"));
            }
            let timestamp = effective_activity_timestamp(
                turn.get("projection")
                    .and_then(Value::as_object)
                    .and_then(|projection| projection.get("timestamp")),
                turn.get("createdAt").and_then(Value::as_u64).unwrap_or(0),
            );
            insert_activity_interval(
                &mut intervals,
                timestamp,
                fallback_timestamp_ms,
                boundaries_ms,
            );
        }
        if row_count < INDEX_PAGE_ROWS {
            break;
        }
        if page + 1 == MAX_SCAN_PAGES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "legacy Turn activity scan exceeds its page budget",
            ));
        }
        start = continuation;
    }
    if legacy_rows != lane_count {
        return Err(invalid_data("Turn lane count is inconsistent"));
    }
    Ok(intervals)
}

fn sync_head(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<u64> {
    let key = entity_key(
        transaction,
        CONVERSATION_SYNC_HEAD_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )?;
    decode_u64(
        database.entity_get(transaction, &key)?,
        "conversation sync head is malformed",
    )
}

fn load_attempt_for_update(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
) -> io::Result<Option<LoadedAttemptForUpdate>> {
    let claim_key = global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)?;
    let Some(locator) = database.entity_get(transaction, &claim_key)? else {
        return Ok(None);
    };
    let (key, located_conversation_id) = match decode_attempt_locator(&locator)? {
        AttemptLocator::LegacyOwner(owner_user_id) => {
            if owner_user_id != transaction.owner_user_id() {
                return Ok(None);
            }
            (legacy_attempt_key(transaction, attempt_id)?, None)
        }
        AttemptLocator::Conversation {
            owner_user_id,
            conversation_id,
        } => {
            if owner_user_id != transaction.owner_user_id() {
                return Ok(None);
            }
            (
                attempt_key(transaction, &conversation_id, attempt_id)?,
                Some(conversation_id),
            )
        }
    };
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let physical_version = crate::versioned_document::stored_document_version(
        &stored,
        "generation_attempts",
        attempt_id,
    )?;
    let (_, document_json) = crate::versioned_document::materialize_stored_document(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        "generation_attempts",
    )?;
    let document = serde_json::from_slice::<Value>(&document_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("attempt document is malformed"))?;
    let conversation_id = document
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt conversation identity is malformed"))?
        .to_owned();
    if located_conversation_id
        .as_deref()
        .is_some_and(|located| located != conversation_id)
    {
        return Err(invalid_data(
            "attempt locator conversation identity differs",
        ));
    }
    if crate::conversation_header::sync_header(database, transaction, &conversation_id)?.is_none() {
        return Ok(None);
    }
    Ok(Some(LoadedAttemptForUpdate {
        key,
        physical_version,
        document,
        conversation_id,
    }))
}

fn load_current_turn_for_attempt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    attempt: &Map<String, Value>,
    conversation_id: &str,
) -> io::Result<Option<LoadedTurnForAttempt>> {
    let turn_id = attempt
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?;
    let key = turn_key(transaction, conversation_id, turn_id)?;
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let physical_version = crate::versioned_document::stored_document_version(
        &stored,
        DOCUMENT_IDENTITY,
        &turn_logical_key(conversation_id, turn_id),
    )?;
    let (_, physical_json) = crate::versioned_document::materialize_stored_document(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        DOCUMENT_IDENTITY,
    )?;
    let physical_document = decode_turn_value(&physical_json)?;
    let projection_head = projection_head_from_document(&physical_document)?;
    let mut document = physical_document;
    if let Some(head) = &projection_head {
        materialize_projection_head(database, transaction, &mut document, head)?;
    }
    if document.get("currentAttemptId").and_then(Value::as_str) != Some(attempt_id) {
        return Ok(None);
    }
    Ok(Some(LoadedTurnForAttempt {
        key,
        physical_version,
        document,
        projection_head,
    }))
}

fn store_attempt_document(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    loaded: &LoadedAttemptForUpdate,
    document: &Map<String, Value>,
    committed_at_ms: u64,
) -> io::Result<()> {
    if attempt_is_live(&loaded.document) != attempt_is_live(document) {
        let turn_id = document
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?;
        let turn = load_current_turn_for_attempt(
            database,
            transaction,
            attempt_id,
            document,
            &loaded.conversation_id,
        )?
        .ok_or_else(|| invalid_data("attempt owner Turn is missing"))?;
        let lane_id = turn
            .document
            .get("laneId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt lane identity is malformed"))?;
        set_lane_live_attempt(
            database,
            transaction,
            &loaded.conversation_id,
            lane_id,
            turn_id,
            attempt_id,
            attempt_is_live(document),
        )?;
    }
    update_attempt_timing_indexes(database, transaction, &loaded.document, document)?;
    update_attempt_turn_directory(database, transaction, document)?;
    update_recovery_index(database, transaction, &loaded.document, document)?;
    update_attempt_event_retention_index(database, transaction, &loaded.document, document)?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: loaded.key.clone(),
            namespace: "generation_attempts".to_owned(),
            logical_key: attempt_id.to_owned(),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("attempt cannot be encoded"))?,
            expected_version: Some(loaded.physical_version),
            updated_at_ms: committed_at_ms,
        },
    )
    .map(|_| ())
}

fn update_attempt_turn_lifecycle(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    loaded: &LoadedTurnForAttempt,
    conversation_id: &str,
    next_status: Option<&str>,
    updated_at_ms: u64,
    committed_at_ms: u64,
) -> io::Result<(String, Map<String, Value>, u64, u64)> {
    let mut document = loaded.document.clone();
    let turn_id = document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn identity is malformed"))?
        .to_owned();
    let lane_id = document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn lane identity is malformed"))?
        .to_owned();
    let ordinal = document
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
    let previous_updated_at_ms = document
        .get("updatedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn update timestamp is malformed"))?;
    let previous_revision = document
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let next_revision = previous_revision
        .checked_add(1)
        .ok_or_else(|| invalid_data("turn projection revision overflow"))?;
    let projection = document
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let bridge_patch = projection_patch(&projection, &projection, previous_revision)?
        .as_object()
        .cloned()
        .ok_or_else(|| invalid_data("Turn projection bridge is malformed"))?;
    let bridge_patch_bytes = serde_json::to_vec(&bridge_patch)
        .map_err(|_| invalid_data("Turn projection bridge cannot be encoded"))?
        .len();
    let next_projection_head = if let Some(current) = &loaded.projection_head {
        let next_count = current
            .patch_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("Turn projection patch count overflows"))?;
        let next_bytes = current
            .patch_bytes
            .checked_add(bridge_patch_bytes)
            .ok_or_else(|| invalid_data("Turn projection patch bytes overflow"))?;
        if next_count <= MAX_TURN_PROJECTION_HEAD_PATCHES
            && next_bytes <= MAX_TURN_PROJECTION_PATCH_BYTES
        {
            let next = ProjectionHeadDescriptor {
                head_id: current.head_id.clone(),
                attempt_id: current.attempt_id.clone(),
                base_revision: current.base_revision,
                patch_count: next_count,
                patch_bytes: next_bytes,
            };
            store_projection_head_patch(
                database,
                transaction,
                conversation_id,
                &turn_id,
                &next,
                &bridge_patch,
                committed_at_ms,
            )?;
            Some(next)
        } else {
            retire_projection_head(
                database,
                transaction,
                conversation_id,
                &turn_id,
                Some(current),
            )?;
            let checkpoint = ProjectionHeadDescriptor {
                head_id: projection_head_id(&current.attempt_id, next_revision),
                attempt_id: current.attempt_id.clone(),
                base_revision: next_revision,
                patch_count: 0,
                patch_bytes: 0,
            };
            store_projection_checkpoint(
                database,
                transaction,
                conversation_id,
                &turn_id,
                &checkpoint,
                &projection,
                committed_at_ms,
            )?;
            Some(checkpoint)
        }
    } else {
        None
    };
    if let Some(status) = next_status {
        document.insert("status".to_owned(), Value::String(status.to_owned()));
    }
    if let Some(head) = &next_projection_head {
        document.insert("projection".to_owned(), json!({}));
        document.insert("_projectionHead".to_owned(), projection_head_value(head));
    } else {
        document.insert("projection".to_owned(), Value::Object(projection.clone()));
        document.remove("_projectionHead");
    }
    document.insert("projectionRevision".to_owned(), Value::from(next_revision));
    document.insert("updatedAt".to_owned(), Value::from(updated_at_ms));
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: loaded.key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(conversation_id, &turn_id),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("turn cannot be encoded"))?,
            expected_version: Some(loaded.physical_version),
            updated_at_ms: committed_at_ms,
        },
    )?;
    let staged = database
        .entity_get(transaction, &loaded.key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(transaction, conversation_id, &lane_id, ordinal)?,
        staged,
    )?;
    put_lane_compaction_index(database, transaction, &document)?;
    database.entity_delete(
        transaction,
        updated_index_key(
            transaction,
            conversation_id,
            previous_updated_at_ms,
            &turn_id,
        )?,
    )?;
    database.entity_put(
        transaction,
        updated_index_key(transaction, conversation_id, updated_at_ms, &turn_id)?,
        encode_updated_index_value(&turn_id, next_revision)?,
    )?;
    Ok((turn_id, projection, previous_revision, next_revision))
}

fn public_attempt(document: &Map<String, Value>) -> io::Result<Value> {
    if !document.contains_key("commandId") {
        return Ok(Value::Object(document.clone()));
    }
    let required_text = |field: &str| -> io::Result<Value> {
        document
            .get(field)
            .and_then(Value::as_str)
            .map(|value| Value::String(value.to_owned()))
            .ok_or_else(|| invalid_data("attempt public identity is malformed"))
    };
    let mut public = Map::from_iter([
        ("attemptId".to_owned(), required_text("attemptId")?),
        (
            "conversationId".to_owned(),
            required_text("conversationId")?,
        ),
        ("turnId".to_owned(), required_text("turnId")?),
        ("commandId".to_owned(), required_text("commandId")?),
        ("taskId".to_owned(), required_text("taskId")?),
        ("operation".to_owned(), required_text("operation")?),
        ("status".to_owned(), required_text("status")?),
        (
            "baseProjectionRevision".to_owned(),
            Value::from(
                document
                    .get("baseProjectionRevision")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("attempt base revision is malformed"))?,
            ),
        ),
        (
            "resumeAnchor".to_owned(),
            Value::Object(
                document
                    .get("resumeAnchor")
                    .and_then(Value::as_object)
                    .cloned()
                    .ok_or_else(|| invalid_data("attempt resume anchor is malformed"))?,
            ),
        ),
        (
            "createdAt".to_owned(),
            Value::from(
                document
                    .get("createdAt")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("attempt creation timestamp is malformed"))?,
            ),
        ),
        (
            "startedAt".to_owned(),
            document.get("startedAt").cloned().unwrap_or(Value::Null),
        ),
        (
            "settledAt".to_owned(),
            document.get("settledAt").cloned().unwrap_or(Value::Null),
        ),
    ]);
    let queue_id = document
        .get("_queueId")
        .and_then(Value::as_str)
        .unwrap_or("");
    let queue_state = document
        .get("_queueState")
        .and_then(Value::as_str)
        .unwrap_or("");
    if !queue_id.is_empty()
        && queue_state == "pending"
        && document.get("status").and_then(Value::as_str) == Some("pending")
    {
        public.insert(
            "queueBinding".to_owned(),
            json!({"queueId": queue_id, "state": "pending"}),
        );
    }
    Ok(Value::Object(public))
}

pub(crate) fn attempt_dispatchable_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    created_before_ms: u64,
    limit: usize,
) -> io::Result<Vec<u8>> {
    if transaction.owner_user_id() != TENANT_GLOBAL_OWNER_ID
        || !(1..=MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY).contains(&limit)
    {
        return Err(invalid_input("invalid attempt dispatchable query scope"));
    }
    let start = EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
        b"",
    )?;
    let exclusive_cutoff = created_before_ms
        .checked_add(1)
        .ok_or_else(|| invalid_input("attempt dispatchable cutoff overflows"))?;
    let end = EntityKey::new(
        transaction.tenant_id(),
        TENANT_GLOBAL_OWNER_ID,
        ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
        &exclusive_cutoff.to_be_bytes(),
    )?;
    let rows = database.entity_scan(transaction, &start, &end, limit)?;
    let mut response = Vec::with_capacity(rows.len());
    let mut response_bytes = 2_usize;
    for (index_key, raw_locator) in rows {
        let locator = serde_json::from_slice::<Value>(&raw_locator)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("attempt dispatchable index is malformed"))?;
        let owner_user_id = locator
            .get("userId")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0 && *value != TENANT_GLOBAL_OWNER_ID)
            .ok_or_else(|| invalid_data("attempt dispatchable owner is malformed"))?;
        let required_locator_text = |field: &str| {
            locator
                .get(field)
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| invalid_data("attempt dispatchable identity is malformed"))
        };
        let conversation_id = required_locator_text("conversationId")?;
        let turn_id = required_locator_text("turnId")?;
        let attempt_id = required_locator_text("attemptId")?;
        let created_at_ms = u64::from_be_bytes(
            index_key
                .key_bytes()
                .get(..8)
                .ok_or_else(|| invalid_data("attempt dispatchable key is malformed"))?
                .try_into()
                .unwrap(),
        );
        if index_key != dispatchable_index_key(transaction, created_at_ms, attempt_id)? {
            return Err(invalid_data("attempt dispatchable key identity differs"));
        }
        for namespace in [
            GENERATION_ATTEMPT_NAMESPACE,
            TURN_DOCUMENT_NAMESPACE,
            CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
        ] {
            database.authorize_entity_namespace_for_owner(transaction, owner_user_id, namespace)?;
        }
        let attempt_key = attempt_key_for_owner(
            transaction.tenant_id(),
            owner_user_id,
            conversation_id,
            attempt_id,
        )?;
        let stored_attempt = database
            .entity_get(transaction, &attempt_key)?
            .ok_or_else(|| invalid_data("attempt dispatchable target is missing"))?;
        crate::versioned_document::stored_document_version(
            &stored_attempt,
            "generation_attempts",
            attempt_id,
        )?;
        let (_, attempt_json) = crate::versioned_document::materialize_stored_document(
            database,
            transaction.tenant_id(),
            owner_user_id,
            &stored_attempt,
            "generation_attempts",
        )?;
        let attempt = serde_json::from_slice::<Value>(&attempt_json)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("attempt dispatchable document is malformed"))?;
        if attempt.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
            || attempt.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
            || attempt.get("turnId").and_then(Value::as_str) != Some(turn_id)
            || attempt.get("createdAt").and_then(Value::as_u64) != Some(created_at_ms)
            || attempt.get("status").and_then(Value::as_str) != Some("pending")
            || attempt.get("taskId").and_then(Value::as_str) != Some("")
            || attempt.get("_queueState").and_then(Value::as_str) != Some("")
            || attempt.get("_dispatchMode").and_then(Value::as_str) != Some("conversation_executor")
        {
            return Err(invalid_data("attempt dispatchable index target differs"));
        }
        let stored_turn = database
            .entity_get(
                transaction,
                &turn_key_for_owner(
                    transaction.tenant_id(),
                    owner_user_id,
                    conversation_id,
                    turn_id,
                )?,
            )?
            .ok_or_else(|| invalid_data("attempt dispatchable Turn is missing"))?;
        let (_, turn_json) = crate::versioned_document::materialize_stored_document(
            database,
            transaction.tenant_id(),
            owner_user_id,
            &stored_turn,
            DOCUMENT_IDENTITY,
        )?;
        let turn = decode_turn_value(&turn_json)?;
        if turn.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
            || turn.get("turnId").and_then(Value::as_str) != Some(turn_id)
            || turn.get("status").and_then(Value::as_str) != Some("pending")
            || turn.get("currentAttemptId").and_then(Value::as_str) != Some(attempt_id)
        {
            return Err(invalid_data("attempt dispatchable Turn target differs"));
        }
        let epoch_key = EntityKey::new(
            transaction.tenant_id(),
            owner_user_id,
            CONVERSATION_EXECUTION_EPOCH_NAMESPACE,
            conversation_id.as_bytes(),
        )?;
        let execution_epoch = match database.entity_get(transaction, &epoch_key)? {
            None => 0,
            Some(raw) if raw.len() == 8 => u64::from_le_bytes(raw.try_into().unwrap()),
            Some(_) => return Err(invalid_data("conversation execution epoch is malformed")),
        };
        let config = attempt
            .get("_config")
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| invalid_data("attempt config is malformed"))?;
        let item = json!({
            "userId": owner_user_id,
            "turn": public_turn(turn, execution_epoch)?,
            "attempt": public_attempt(&attempt)?,
            "config": config,
        });
        let projected_bytes = serde_json::to_vec(&item)
            .map_err(|_| invalid_data("attempt dispatchable response cannot be encoded"))?
            .len();
        if response_bytes
            .checked_add(projected_bytes)
            .and_then(|bytes| bytes.checked_add(usize::from(!response.is_empty())))
            .is_none_or(|bytes| bytes > MAX_TRANSACTION_IR_LITERAL_BYTES)
        {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "attempt dispatchable response exceeds 8 MiB",
            ));
        }
        response_bytes += projected_bytes + usize::from(!response.is_empty());
        response.push(item);
    }
    encode_response(&Value::Array(response))
}

pub(crate) fn attempt_dispatch_worker(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AttemptDispatchWorkerRequest,
) -> io::Result<Vec<u8>> {
    if request.user_id != transaction.owner_user_id() {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "conversation worker dispatch escaped its owner scope",
        ));
    }
    let Some(loaded) = load_attempt_for_update(database, transaction, &request.attempt_id)? else {
        return encode_response(&Value::Null);
    };
    let Some(turn) = load_current_turn_for_attempt(
        database,
        transaction,
        &request.attempt_id,
        &loaded.document,
        &loaded.conversation_id,
    )?
    else {
        return encode_response(&Value::Null);
    };
    let task_id = format!("conversation-attempt:{}", request.attempt_id);
    let job_payload = json!({
        "contract": "tofu.conversation-attempt-job/v1",
        "conversationId": loaded.conversation_id,
        "turnId": loaded.document.get("turnId").and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt Turn identity is malformed"))?,
        "attemptId": request.attempt_id,
        "principal": request.principal,
        "baseProjectionRevision": loaded.document.get("baseProjectionRevision")
            .and_then(Value::as_u64).unwrap_or(0),
        "operation": loaded.document.get("operation").and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt operation is malformed"))?,
    });
    let payload_json = serde_json::to_vec(&job_payload)
        .map_err(|_| invalid_data("conversation worker payload cannot be encoded"))?;
    if payload_json.len() > MAX_WORKER_JOB_PAYLOAD_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("storage_payload_too_large: worker job payload exceeds {MAX_WORKER_JOB_PAYLOAD_BYTES} bytes"),
        ));
    }
    let digest_input = serde_json::to_vec(&json!({
        "tenantId": request.tenant_label,
        "taskKind": "conversation-attempt",
        "payload": job_payload,
    }))
    .map_err(|_| invalid_data("conversation worker digest cannot be encoded"))?;
    let request_digest = format!("{:x}", Sha256::digest(digest_input));
    let existing_task_id = loaded
        .document
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt task identity is malformed"))?;
    let replay = if existing_task_id.is_empty() {
        if loaded.document.get("status").and_then(Value::as_str) != Some("pending")
            || turn.document.get("status").and_then(Value::as_str) != Some("pending")
        {
            return Err(conflict("conversation attempt is not dispatchable"));
        }
        false
    } else if existing_task_id == task_id {
        if crate::worker_job::get(database, transaction, &task_id, request.user_id)?.is_none() {
            return Err(conflict(
                "conversation attempt binding has no durable worker job",
            ));
        }
        true
    } else {
        return Err(conflict(
            "conversation attempt is already bound to another executor",
        ));
    };
    let enqueued = crate::worker_job::enqueue(
        database,
        transaction,
        crate::worker_job::EnqueueRequest {
            task_id: task_id.clone(),
            user_id: request.user_id,
            tenant_id: request.tenant_label.clone(),
            task_kind: "conversation-attempt".to_owned(),
            payload: job_payload,
            idempotency_key: task_id.clone(),
            request_digest,
            priority: request.priority,
            available_at_ms: request.now_ms,
            now_ms: request.now_ms,
        },
    )?;
    let enqueued = serde_json::from_slice::<Value>(&enqueued)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("worker enqueue response is malformed"))?;
    let job = enqueued
        .get("job")
        .cloned()
        .ok_or_else(|| invalid_data("worker enqueue job is missing"))?;
    let attempt = if replay {
        public_attempt(&loaded.document)?
    } else {
        let bound = attempt_bind(
            database,
            transaction,
            &request.attempt_id,
            &task_id,
            "",
            request.now_ms,
        )?;
        let bound: Value = serde_json::from_slice(&bound)
            .map_err(|_| invalid_data("attempt bind response is malformed"))?;
        if bound.is_null() {
            return Err(conflict(
                "conversation attempt disappeared during worker dispatch",
            ));
        }
        bound
    };
    let mut response = Map::from_iter([
        (
            "created".to_owned(),
            Value::Bool(!replay && enqueued.get("created") == Some(&Value::Bool(true))),
        ),
        ("attempt".to_owned(), attempt),
        ("job".to_owned(), job),
    ]);
    if replay {
        response.insert("idempotentReplay".to_owned(), Value::Bool(true));
    }
    encode_response(&Value::Object(response))
}

fn resume_option_anchors(
    settlement: &Map<String, Value>,
) -> io::Result<BTreeMap<String, Map<String, Value>>> {
    let options = match settlement.get("resumeOptions") {
        None => &[][..],
        Some(Value::Array(options)) => options.as_slice(),
        Some(_) => return Err(invalid_data("invalid stored resume options")),
    };
    let mut anchors = BTreeMap::new();
    for option in options {
        let (operation, anchor) = match option {
            Value::String(operation) if !operation.is_empty() => (operation.clone(), Map::new()),
            Value::Object(option) => {
                let operation = option
                    .get("operation")
                    .and_then(Value::as_str)
                    .filter(|operation| !operation.is_empty())
                    .ok_or_else(|| invalid_data("invalid stored resume operation"))?
                    .to_owned();
                let anchor = match option.get("anchor") {
                    None => Map::new(),
                    Some(Value::Object(anchor)) => anchor.clone(),
                    Some(_) => return Err(invalid_data("invalid stored resume anchor")),
                };
                (operation, anchor)
            }
            _ => return Err(invalid_data("invalid stored resume option")),
        };
        if anchors.insert(operation, anchor).is_some() {
            return Err(invalid_data("duplicate stored resume operation"));
        }
    }
    Ok(anchors)
}

fn json_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => value.as_f64() != Some(0.0),
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

fn stamp_execution_identity(
    value: &mut Value,
    attempt_id: &str,
    task_id: &str,
    include_segment: bool,
) {
    let Some(object) = value.as_object_mut() else {
        return;
    };
    if object
        .get("attemptId")
        .is_none_or(|value| !json_truthy(value))
    {
        object.insert("attemptId".to_owned(), Value::String(attempt_id.to_owned()));
    }
    if !task_id.is_empty() && object.get("taskId").is_none_or(|value| !json_truthy(value)) {
        object.insert("taskId".to_owned(), Value::String(task_id.to_owned()));
    }
    if include_segment {
        if let Some(round) = object.get_mut("_round") {
            stamp_execution_identity(round, attempt_id, task_id, false);
        }
    }
}

fn projection_history_with_execution_identity(
    projection: &mut Map<String, Value>,
    attempt_id: &str,
    task_id: &str,
) {
    if let Some(Value::Array(rounds)) = projection.get_mut("toolRounds") {
        for round in rounds {
            stamp_execution_identity(round, attempt_id, task_id, false);
        }
    }
    if let Some(Value::Array(segments)) = projection.get_mut("segments") {
        for segment in segments {
            let should_stamp = segment.get("type").and_then(Value::as_str) == Some("tool_use")
                || segment
                    .get("llmRound")
                    .is_some_and(|value| !value.is_null());
            if should_stamp {
                stamp_execution_identity(segment, attempt_id, task_id, true);
            }
        }
    }
}

struct AttemptTurnTransition<'a> {
    conversation_id: &'a str,
    turn_id: &'a str,
    attempt_id: &'a str,
    actor: &'a str,
    kind: &'a str,
    projection: Map<String, Value>,
    updated_at_ms: u64,
    committed_at_ms: u64,
}

fn stage_attempt_turn_transition(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    stored: &[u8],
    transition: AttemptTurnTransition<'_>,
) -> io::Result<StagedProjectionUpdate> {
    let AttemptTurnTransition {
        conversation_id,
        turn_id,
        attempt_id,
        actor,
        kind,
        projection,
        updated_at_ms,
        committed_at_ms,
    } = transition;
    let physical_version = crate::versioned_document::stored_document_version(
        stored,
        DOCUMENT_IDENTITY,
        &turn_logical_key(conversation_id, turn_id),
    )?;
    let mut document = materialize_turn(database, transaction, stored)?;
    let previous_projection = document
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let previous_revision = document
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let next_revision = previous_revision
        .checked_add(1)
        .ok_or_else(|| invalid_data("turn projection revision overflow"))?;
    let lane_id = document
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn lane identity is malformed"))?
        .to_owned();
    let ordinal = document
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
    let previous_updated_at_ms = document
        .get("updatedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn update timestamp is malformed"))?;
    let created_at_ms = document
        .get("createdAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn creation timestamp is malformed"))?;
    document.insert("status".to_owned(), Value::String("pending".to_owned()));
    document.insert(
        "currentAttemptId".to_owned(),
        Value::String(attempt_id.to_owned()),
    );
    document.insert("projection".to_owned(), Value::Object(projection.clone()));
    document.insert("projectionRevision".to_owned(), Value::from(next_revision));
    document.insert("settlement".to_owned(), Value::Object(Map::new()));
    document.insert("actor".to_owned(), Value::String(actor.to_owned()));
    document.insert("kind".to_owned(), Value::String(kind.to_owned()));
    document.insert("updatedAt".to_owned(), Value::from(updated_at_ms));
    let key = turn_key(transaction, conversation_id, turn_id)?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(conversation_id, turn_id),
            value_json: serde_json::to_vec(&Value::Object(document.clone()))
                .map_err(|_| invalid_data("turn cannot be encoded"))?,
            expected_version: Some(physical_version),
            updated_at_ms: committed_at_ms,
        },
    )?;
    let staged = database
        .entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(transaction, conversation_id, &lane_id, ordinal)?,
        staged,
    )?;
    put_lane_compaction_index(database, transaction, &document)?;
    if lane_id == "main" {
        database.entity_put(
            transaction,
            activity_index_key(transaction, conversation_id, ordinal)?,
            encode_activity_index_value(effective_activity_timestamp(
                projection.get("timestamp"),
                created_at_ms,
            )),
        )?;
    }
    database.entity_delete(
        transaction,
        updated_index_key(
            transaction,
            conversation_id,
            previous_updated_at_ms,
            turn_id,
        )?,
    )?;
    database.entity_put(
        transaction,
        updated_index_key(transaction, conversation_id, updated_at_ms, turn_id)?,
        encode_updated_index_value(turn_id, next_revision)?,
    )?;
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        conversation_id,
    )?;
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    Ok(StagedProjectionUpdate {
        turn: public_turn(document, execution_epoch)?,
        before: previous_projection,
        after: projection,
    })
}

pub(crate) fn attempt_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let claim_key = global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)?;
    let Some(locator) = database.entity_get(transaction, &claim_key)? else {
        return Ok(None);
    };
    let (key, located_conversation_id) = match decode_attempt_locator(&locator)? {
        AttemptLocator::LegacyOwner(owner_user_id) => {
            if owner_user_id != transaction.owner_user_id() {
                return Ok(None);
            }
            (legacy_attempt_key(transaction, attempt_id)?, None)
        }
        AttemptLocator::Conversation {
            owner_user_id,
            conversation_id,
        } => {
            if owner_user_id != transaction.owner_user_id() {
                return Ok(None);
            }
            if crate::conversation_header::sync_header(database, transaction, &conversation_id)?
                .is_none()
            {
                return Ok(None);
            }
            (
                attempt_key(transaction, &conversation_id, attempt_id)?,
                Some(conversation_id),
            )
        }
    };
    let Some(envelope) = crate::versioned_document::get(
        database,
        transaction,
        &key,
        "generation_attempts",
        attempt_id,
    )?
    else {
        return Ok(None);
    };
    let value = serde_json::from_slice::<Value>(&envelope)
        .ok()
        .and_then(|value| value.get("value").cloned())
        .ok_or_else(|| invalid_data("attempt document is malformed"))?;
    let conversation_id = value
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt conversation identity is malformed"))?;
    if located_conversation_id
        .as_deref()
        .is_some_and(|located| located != conversation_id)
    {
        return Err(invalid_data(
            "attempt locator conversation identity differs",
        ));
    }
    if located_conversation_id.is_none()
        && crate::conversation_header::sync_header(database, transaction, conversation_id)?
            .is_none()
    {
        return Ok(None);
    }
    let document = value
        .as_object()
        .ok_or_else(|| invalid_data("attempt document is malformed"))?;
    if located_conversation_id.is_some() {
        let turn_id = document
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?;
        if database
            .entity_get(
                transaction,
                &turn_key(transaction, conversation_id, turn_id)?,
            )?
            .is_none()
        {
            return Ok(None);
        }
    }
    encode_response(&public_attempt(document)?).map(Some)
}

fn decode_attempt_timing_index(raw: &[u8]) -> io::Result<Map<String, Value>> {
    let record = serde_json::from_slice::<Value>(raw)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("attempt timing index is malformed"))?;
    for field in ["attemptId", "conversationId", "turnId", "taskId", "status"] {
        if record
            .get(field)
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        {
            return Err(invalid_data("attempt timing index identity is malformed"));
        }
    }
    if record.get("createdAt").and_then(Value::as_u64).is_none()
        || record.get("effectiveAt").and_then(Value::as_u64).is_none()
    {
        return Err(invalid_data("attempt timing index timestamp is malformed"));
    }
    match record.get("settledAt") {
        None | Some(Value::Null) => {}
        Some(Value::Number(value)) if value.as_u64().is_some() => {}
        Some(_) => return Err(invalid_data("attempt timing index timestamp is malformed")),
    }
    Ok(record)
}

const TRACE_MAX_SPANS: usize = 256;
const TRACE_MAX_GAPS: usize = 128;
const TRACE_MAX_STATUS_ENTRIES: usize = 128;

fn trace_text(value: &Value, maximum: usize) -> String {
    let text = match value {
        Value::String(value) => value.clone(),
        Value::Null => String::new(),
        other => serde_json::to_string(other).unwrap_or_default(),
    };
    text.chars().take(maximum).collect()
}

fn sanitize_trace_value(value: &Value, depth: usize) -> Value {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        Value::String(value) => Value::String(value.chars().take(400).collect()),
        Value::Object(object) if depth < 5 => Value::Object(
            object
                .iter()
                .filter(|(key, _)| !key.starts_with('_'))
                .take(32)
                .filter_map(|(key, child)| {
                    let key: String = key.chars().take(100).collect();
                    (!key.is_empty()).then(|| (key, sanitize_trace_value(child, depth + 1)))
                })
                .collect(),
        ),
        Value::Array(items) if depth < 5 => Value::Array(
            items
                .iter()
                .take(32)
                .map(|item| sanitize_trace_value(item, depth + 1))
                .collect(),
        ),
        other => Value::String(trace_text(other, 200)),
    }
}

fn bounded_trace_rows(
    value: Option<&Value>,
    maximum: usize,
    keep_recent: bool,
) -> (Vec<Value>, usize) {
    let rows = value
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|row| row.is_object())
        .map(|row| sanitize_trace_value(row, 0))
        .collect::<Vec<_>>();
    if rows.len() <= maximum {
        return (rows, 0);
    }
    let dropped = rows.len() - maximum;
    if keep_recent {
        return (rows[dropped..].to_vec(), dropped);
    }
    let head = maximum / 2;
    let tail = maximum - head;
    (
        rows[..head]
            .iter()
            .chain(rows[rows.len() - tail..].iter())
            .cloned()
            .collect(),
        dropped,
    )
}

fn add_trace_dropped(trace: &mut Map<String, Value>, field: &str, count: usize) {
    if count == 0 {
        return;
    }
    let previous = trace.get(field).and_then(Value::as_u64).unwrap_or(0);
    trace.insert(
        field.to_owned(),
        Value::from(
            previous
                .saturating_add(count as u64)
                .min(MAX_TIMING_TRACE_COUNTER),
        ),
    );
    trace.insert("compacted".to_owned(), Value::Bool(true));
}

fn trace_encoded_len(trace: &Map<String, Value>) -> io::Result<usize> {
    serde_json::to_vec(trace)
        .map(|encoded| encoded.len())
        .map_err(|_| invalid_data("timing trace cannot be encoded"))
}

fn trim_trace_lane(
    trace: &mut Map<String, Value>,
    lane: &str,
    floor: usize,
    dropped_field: &str,
    keep_recent: bool,
) -> io::Result<()> {
    while trace_encoded_len(trace)? > MAX_TIMING_TRACE_PERSISTED_BYTES {
        let removed = {
            let Some(rows) = trace.get_mut(lane).and_then(Value::as_array_mut) else {
                break;
            };
            if rows.len() <= floor {
                break;
            }
            let remove = (rows.len() / 4).max(1).min(rows.len() - floor);
            let start = if keep_recent {
                0
            } else {
                (rows.len() - remove) / 2
            };
            rows.drain(start..start + remove);
            remove
        };
        add_trace_dropped(trace, dropped_field, removed);
    }
    Ok(())
}

fn compact_timing_trace(document: &Map<String, Value>) -> io::Result<Map<String, Value>> {
    let mut out = Map::new();
    for field in [
        "version",
        "taskId",
        "eventsAvailable",
        "eventLogAvailable",
        "status",
        "running",
        "coverage",
        "coverageReason",
        "tStart",
        "tEnd",
        "totalMs",
    ] {
        if let Some(value) = document.get(field) {
            out.insert(field.to_owned(), sanitize_trace_value(value, 0));
        }
    }
    if let Some(source) = document.get("source") {
        let source = trace_text(source, 40);
        if !source.is_empty() {
            out.insert("source".to_owned(), Value::String(source));
        }
    }
    for flag in ["detailCompacted", "compacted"] {
        if document.get(flag).and_then(Value::as_bool) == Some(true) {
            out.insert(flag.to_owned(), Value::Bool(true));
        }
    }
    let mut dropped_over_budget = 0;
    if let Some(summary) = document.get("summary").and_then(Value::as_object) {
        let mut summary = sanitize_trace_value(&Value::Object(summary.clone()), 0)
            .as_object()
            .cloned()
            .unwrap_or_default();
        if let Some(over_budget) = document
            .get("summary")
            .and_then(Value::as_object)
            .and_then(|summary| summary.get("overBudget"))
        {
            let (rows, dropped) = bounded_trace_rows(Some(over_budget), TRACE_MAX_SPANS, false);
            summary.insert("overBudget".to_owned(), Value::Array(rows));
            dropped_over_budget = dropped;
        }
        out.insert("summary".to_owned(), Value::Object(summary));
    }
    for (lane, maximum, recent, dropped_field) in [
        ("spans", TRACE_MAX_SPANS, false, "droppedSpans"),
        ("gaps", TRACE_MAX_GAPS, false, "droppedGaps"),
        (
            "statusHistory",
            TRACE_MAX_STATUS_ENTRIES,
            true,
            "statusDroppedCount",
        ),
        (
            "clientObservations",
            MAX_TIMING_TRACE_CLIENT_OBSERVATIONS,
            true,
            "clientObservationDroppedCount",
        ),
    ] {
        let (rows, dropped) = bounded_trace_rows(document.get(lane), maximum, recent);
        if !rows.is_empty() || document.contains_key(lane) {
            out.insert(lane.to_owned(), Value::Array(rows));
        }
        let prior = document
            .get(dropped_field)
            .and_then(Value::as_u64)
            .unwrap_or(0)
            .min(MAX_TIMING_TRACE_COUNTER);
        if prior > 0 {
            out.insert(dropped_field.to_owned(), Value::from(prior));
        }
        add_trace_dropped(&mut out, dropped_field, dropped);
    }
    let prior_over_budget = document
        .get("overBudgetDroppedCount")
        .and_then(Value::as_u64)
        .unwrap_or(0)
        .min(MAX_TIMING_TRACE_COUNTER);
    if prior_over_budget > 0 {
        out.insert(
            "overBudgetDroppedCount".to_owned(),
            Value::from(prior_over_budget),
        );
    }
    add_trace_dropped(&mut out, "overBudgetDroppedCount", dropped_over_budget);

    for (lane, floor, dropped, recent) in [
        ("spans", 24, "droppedSpans", false),
        ("statusHistory", 16, "statusDroppedCount", true),
        ("gaps", 16, "droppedGaps", false),
        (
            "clientObservations",
            16,
            "clientObservationDroppedCount",
            true,
        ),
    ] {
        trim_trace_lane(&mut out, lane, floor, dropped, recent)?;
    }
    if trace_encoded_len(&out)? > MAX_TIMING_TRACE_PERSISTED_BYTES {
        if let Some(spans) = out.get_mut("spans").and_then(Value::as_array_mut) {
            for span in spans {
                if let Some(span) = span.as_object_mut() {
                    if span.contains_key("attrs") {
                        span.insert("attrs".to_owned(), json!({}));
                    }
                }
            }
        }
        out.insert("detailCompacted".to_owned(), Value::Bool(true));
        out.insert("compacted".to_owned(), Value::Bool(true));
    }
    for (lane, floor, dropped, recent) in [
        ("gaps", 0, "droppedGaps", false),
        ("spans", 1, "droppedSpans", false),
        ("statusHistory", 1, "statusDroppedCount", true),
        (
            "clientObservations",
            1,
            "clientObservationDroppedCount",
            true,
        ),
    ] {
        trim_trace_lane(&mut out, lane, floor, dropped, recent)?;
    }
    while trace_encoded_len(&out)? > MAX_TIMING_TRACE_PERSISTED_BYTES {
        let removed = out
            .get_mut("summary")
            .and_then(Value::as_object_mut)
            .and_then(|summary| summary.get_mut("overBudget"))
            .and_then(Value::as_array_mut)
            .and_then(|rows| (!rows.is_empty()).then(|| rows.remove(rows.len() / 2)))
            .is_some();
        if !removed {
            break;
        }
        add_trace_dropped(&mut out, "overBudgetDroppedCount", 1);
        out.insert("detailCompacted".to_owned(), Value::Bool(true));
    }
    if trace_encoded_len(&out)? > MAX_TIMING_TRACE_PERSISTED_BYTES {
        for (lane, dropped) in [
            ("gaps", "droppedGaps"),
            ("spans", "droppedSpans"),
            ("statusHistory", "statusDroppedCount"),
            ("clientObservations", "clientObservationDroppedCount"),
        ] {
            let removed = out
                .get_mut(lane)
                .and_then(Value::as_array_mut)
                .map_or(0, |rows| {
                    let length = rows.len();
                    rows.clear();
                    length
                });
            add_trace_dropped(&mut out, dropped, removed);
        }
        if let Some(summary) = out.get_mut("summary").and_then(Value::as_object_mut) {
            let aggregate = [
                "totalMs",
                "llmMs",
                "toolMs",
                "waitMs",
                "compactionMs",
                "approvalWaitMs",
                "unattributedMs",
                "ttftMs",
            ]
            .into_iter()
            .filter_map(|key| {
                summary
                    .get(key)
                    .cloned()
                    .map(|value| (key.to_owned(), value))
            })
            .collect::<Map<_, _>>();
            *summary = aggregate;
            summary.insert("overBudget".to_owned(), Value::Array(Vec::new()));
        }
        out.insert("detailCompacted".to_owned(), Value::Bool(true));
        out.insert("compacted".to_owned(), Value::Bool(true));
    }
    if trace_encoded_len(&out)? > MAX_TIMING_TRACE_PERSISTED_BYTES {
        return Err(invalid_data("timing trace exceeds its durable byte bound"));
    }
    Ok(out)
}

fn perception_integer(value: &Value, maximum: u64) -> Option<u64> {
    value
        .as_u64()
        .filter(|value| *value <= maximum)
        .or_else(|| {
            let value = value.as_f64()?;
            (value.is_finite() && value >= 0.0 && value <= maximum as f64 && value.fract() == 0.0)
                .then_some(value as u64)
        })
}

pub(crate) fn perception_observation_is_valid(
    observation: &Map<String, Value>,
    attempt_id: &str,
) -> bool {
    const TEXT_FIELDS: [(&str, usize, bool); 8] = [
        ("observationId", 160, true),
        ("attemptId", 128, true),
        ("clientId", 64, true),
        ("phase", 80, false),
        ("detailKey", 160, false),
        ("reason", 160, false),
        ("healthState", 32, false),
        ("visibility", 16, false),
    ];
    const NUMERIC_FIELDS: [(&str, u64); 9] = [
        ("serverEmittedAt", 9_007_199_254_740_991),
        ("receivedAt", 9_007_199_254_740_991),
        ("paintedAt", 9_007_199_254_740_991),
        ("observedAt", 9_007_199_254_740_991),
        ("durationMs", 9_007_199_254_740_991),
        ("generation", MAX_TIMING_TRACE_COUNTER),
        ("projectionRevision", 9_007_199_254_740_991),
        ("retryCount", MAX_TIMING_TRACE_COUNTER),
        ("clientDroppedBefore", MAX_TIMING_TRACE_COUNTER),
    ];
    if observation.keys().any(|field| {
        field != "kind"
            && !TEXT_FIELDS
                .iter()
                .any(|(candidate, _, _)| field == candidate)
            && !NUMERIC_FIELDS
                .iter()
                .any(|(candidate, _)| field == candidate)
    }) {
        return false;
    }
    if TEXT_FIELDS.iter().any(|(field, maximum, required)| {
        observation.get(*field).map_or(*required, |value| {
            value.as_str().is_none_or(|value| {
                (*required && value.is_empty()) || value.chars().count() > *maximum
            })
        })
    }) {
        return false;
    }
    let Some(observation_id) = observation.get("observationId").and_then(Value::as_str) else {
        return false;
    };
    if observation_id.len() > 160
        || !observation_id.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphanumeric()
                || (index > 0 && matches!(byte, b'.' | b'_' | b':' | b'~' | b'-'))
        })
        || observation.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
        || !matches!(
            observation.get("kind").and_then(Value::as_str),
            Some(
                "phase_painted" | "terminal_painted" | "transport_degraded" | "transport_recovered"
            )
        )
        || !matches!(
            observation.get("visibility").and_then(Value::as_str),
            None | Some("visible" | "hidden")
        )
        || !matches!(
            observation.get("healthState").and_then(Value::as_str),
            None | Some(
                "idle" | "connecting" | "live" | "recovering" | "degraded" | "offline" | "closed"
            )
        )
    {
        return false;
    }
    !NUMERIC_FIELDS.iter().any(|(field, maximum)| {
        observation
            .get(*field)
            .is_some_and(|value| perception_integer(value, *maximum).is_none())
    })
}

fn append_perception_observation(
    trace: &Map<String, Value>,
    observation: &Map<String, Value>,
    task_id: &str,
    attempt_id: &str,
    recorded_at_ms: u64,
) -> io::Result<(Map<String, Value>, bool)> {
    if !perception_observation_is_valid(observation, attempt_id) {
        return Err(invalid_input("invalid perception observation"));
    }
    let observation_id = observation["observationId"]
        .as_str()
        .expect("validated observation identity");
    let mut observations = trace
        .get("clientObservations")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_object)
        .cloned()
        .collect::<Vec<_>>();
    if observations
        .iter()
        .any(|row| row.get("observationId").and_then(Value::as_str) == Some(observation_id))
    {
        return compact_timing_trace(trace).map(|trace| (trace, false));
    }
    let mut row = Map::from_iter([
        (
            "observationId".to_owned(),
            Value::String(observation_id.to_owned()),
        ),
        ("kind".to_owned(), observation["kind"].clone()),
        (
            "taskId".to_owned(),
            Value::String(task_id.chars().take(256).collect()),
        ),
        (
            "attemptId".to_owned(),
            Value::String(attempt_id.chars().take(128).collect()),
        ),
        ("clientId".to_owned(), observation["clientId"].clone()),
        ("recordedAt".to_owned(), Value::from(recorded_at_ms)),
    ]);
    for field in ["phase", "detailKey", "reason", "healthState", "visibility"] {
        if let Some(value) = observation.get(field).and_then(Value::as_str) {
            if !value.is_empty() {
                row.insert(field.to_owned(), Value::String(value.to_owned()));
            }
        }
    }
    for (field, maximum) in [
        ("serverEmittedAt", 9_007_199_254_740_991),
        ("receivedAt", 9_007_199_254_740_991),
        ("paintedAt", 9_007_199_254_740_991),
        ("observedAt", 9_007_199_254_740_991),
        ("durationMs", 9_007_199_254_740_991),
        ("generation", MAX_TIMING_TRACE_COUNTER),
        ("projectionRevision", 9_007_199_254_740_991),
        ("retryCount", MAX_TIMING_TRACE_COUNTER),
        ("clientDroppedBefore", MAX_TIMING_TRACE_COUNTER),
    ] {
        if let Some(value) = observation.get(field) {
            row.insert(
                field.to_owned(),
                Value::from(perception_integer(value, maximum).expect("validated integer")),
            );
        }
    }
    if let (Some(received_at), Some(painted_at)) = (
        row.get("receivedAt").and_then(Value::as_u64),
        row.get("paintedAt").and_then(Value::as_u64),
    ) {
        row.insert(
            "renderMs".to_owned(),
            Value::from(painted_at.saturating_sub(received_at).min(600_000)),
        );
    }
    if let (Some(emitted_at), Some(received_at)) = (
        row.get("serverEmittedAt").and_then(Value::as_u64),
        row.get("receivedAt").and_then(Value::as_u64),
    ) {
        let delta = i128::from(received_at) - i128::from(emitted_at);
        if (-60_000..=86_400_000).contains(&delta) {
            row.insert("transportMs".to_owned(), Value::from(delta.max(0) as u64));
            if delta < 0 {
                row.insert("clockSkewSuspected".to_owned(), Value::Bool(true));
            }
        } else {
            row.insert("clockSkewSuspected".to_owned(), Value::Bool(true));
        }
    }
    observations.push(row);
    let overflow = observations
        .len()
        .saturating_sub(MAX_TIMING_TRACE_CLIENT_OBSERVATIONS);
    if overflow > 0 {
        observations.drain(..overflow);
    }
    let mut trace = trace.clone();
    trace.insert("version".to_owned(), Value::from(1));
    trace.insert(
        "taskId".to_owned(),
        Value::String(task_id.chars().take(256).collect()),
    );
    trace.insert(
        "clientObservations".to_owned(),
        Value::Array(observations.into_iter().map(Value::Object).collect()),
    );
    add_trace_dropped(&mut trace, "clientObservationDroppedCount", overflow);
    compact_timing_trace(&trace).map(|trace| (trace, true))
}

pub(crate) fn perception_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &PerceptionRecordRequest,
) -> io::Result<Vec<u8>> {
    let Some(loaded) = load_attempt_for_update(database, transaction, &request.attempt_id)? else {
        return Err(not_found("Turn attempt not found"));
    };
    if loaded.conversation_id != request.conversation_id
        || loaded.document.get("turnId").and_then(Value::as_str) != Some(&request.turn_id)
    {
        return Err(not_found("Turn attempt not found"));
    }
    let task_id = loaded
        .document
        .get("taskId")
        .and_then(Value::as_str)
        .filter(|task_id| !task_id.is_empty())
        .ok_or_else(|| conflict("Turn attempt has no executor task identity"))?
        .to_owned();
    let key = turn_key(transaction, &request.conversation_id, &request.turn_id)?;
    let Some(stored_turn) = database.entity_get(transaction, &key)? else {
        return Err(not_found("Turn attempt not found"));
    };
    let turn = materialize_turn(database, transaction, &stored_turn)?;
    let projection_revision = turn
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let mut trace = loaded
        .document
        .get("_timingTrace")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    if trace.is_empty() {
        if let Some(projected) = turn
            .get("projection")
            .and_then(Value::as_object)
            .and_then(|projection| projection.get("timingTrace"))
            .and_then(Value::as_object)
            .filter(|trace| trace.get("taskId").and_then(Value::as_str) == Some(&task_id))
            .filter(|trace| trace.get("clientObservations").is_some_and(Value::is_array))
        {
            trace.insert("version".to_owned(), Value::from(1));
            trace.insert("taskId".to_owned(), Value::String(task_id.clone()));
            trace.insert(
                "clientObservations".to_owned(),
                projected["clientObservations"].clone(),
            );
            if let Some(dropped) = projected.get("clientObservationDroppedCount") {
                if dropped.as_u64().unwrap_or(0) > 0 {
                    trace.insert("clientObservationDroppedCount".to_owned(), dropped.clone());
                }
            }
        }
    }
    let (trace, applied) = append_perception_observation(
        &trace,
        &request.observation,
        &task_id,
        &request.attempt_id,
        request.recorded_at_ms,
    )?;
    if applied {
        let mut attempt = loaded.document.clone();
        attempt.insert("_timingTrace".to_owned(), Value::Object(trace));
        store_attempt_document(
            database,
            transaction,
            &request.attempt_id,
            &loaded,
            &attempt,
            request.recorded_at_ms,
        )?;
    }
    let conversation_revision =
        crate::conversation_header::revision(database, transaction, &request.conversation_id)?;
    let mut response = Map::from_iter([
        ("applied".to_owned(), Value::Bool(applied)),
        (
            "conversationRevision".to_owned(),
            Value::from(conversation_revision),
        ),
        (
            "projectionRevision".to_owned(),
            Value::from(projection_revision),
        ),
    ]);
    if !applied {
        response.insert("idempotentReplay".to_owned(), Value::Bool(true));
    }
    encode_response(&Value::Object(response))
}

pub(crate) fn timing_trace_get(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
) -> io::Result<Option<Vec<u8>>> {
    let mut prefix = Vec::with_capacity(2 + task_id.len());
    push_text_key(&mut prefix, task_id)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_TIMING_TASK_INDEX_NAMESPACE,
        &prefix,
    )?;
    let candidates = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_TIMING_TRACE_TASK_CANDIDATES + 1,
    )?;
    for (position, (_, raw)) in candidates.iter().enumerate() {
        if position == MAX_TIMING_TRACE_TASK_CANDIDATES {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "timing trace task candidates exceed 100 rows",
            ));
        }
        let indexed = decode_attempt_timing_index(raw)?;
        if indexed.get("taskId").and_then(Value::as_str) != Some(task_id) {
            return Err(invalid_data("timing trace task index escaped its prefix"));
        }
        let attempt_id = indexed
            .get("attemptId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("timing trace attempt identity is malformed"))?;
        let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? else {
            continue;
        };
        let attempt = &loaded.document;
        let turn_id = attempt
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("timing trace Turn identity is malformed"))?;
        if attempt.get("taskId").and_then(Value::as_str) != Some(task_id)
            || indexed.get("conversationId").and_then(Value::as_str)
                != Some(loaded.conversation_id.as_str())
            || indexed.get("turnId").and_then(Value::as_str) != Some(turn_id)
            || indexed.get("status").and_then(Value::as_str)
                != attempt.get("status").and_then(Value::as_str)
        {
            return Err(invalid_data("timing trace index target differs"));
        }
        let mut timing_trace = attempt
            .get("_timingTrace")
            .and_then(Value::as_object)
            .filter(|trace| !trace.is_empty())
            .cloned();
        if timing_trace.is_none() {
            let turn_key = turn_key(transaction, &loaded.conversation_id, turn_id)?;
            let Some(stored_turn) = database.entity_get(transaction, &turn_key)? else {
                continue;
            };
            let turn = materialize_turn(database, transaction, &stored_turn)?;
            timing_trace = turn
                .get("projection")
                .and_then(Value::as_object)
                .and_then(|projection| projection.get("timingTrace"))
                .and_then(Value::as_object)
                .filter(|trace| trace.get("taskId").and_then(Value::as_str) == Some(task_id))
                .cloned();
        }
        let Some(timing_trace) = timing_trace else {
            return Ok(None);
        };
        return encode_response(&json!({
            "attemptId": attempt_id,
            "attemptStatus": attempt.get("status").and_then(Value::as_str)
                .ok_or_else(|| invalid_data("timing trace attempt status is malformed"))?,
            "turnId": turn_id,
            "timingTrace": timing_trace,
        }))
        .map(Some);
    }
    Ok(None)
}

pub(crate) fn timing_trace_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    before_created_at: Option<u64>,
    limit: usize,
) -> io::Result<Vec<u8>> {
    if !(1..=MAX_TIMING_TRACE_ROWS_PER_QUERY).contains(&limit) {
        return Err(invalid_input("invalid timing trace list limit"));
    }
    if before_created_at == Some(0) {
        return encode_response(&json!({"records": [], "has_more": false}));
    }
    let prefix = conversation_prefix(conversation_id)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE,
        &prefix,
    )?;
    let start = match before_created_at {
        None => EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE,
            &prefix,
        )?,
        Some(before) => {
            let mut raw = prefix.clone();
            raw.extend_from_slice(&(!(before - 1)).to_be_bytes());
            EntityKey::new(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE,
                &raw,
            )?
        }
    };
    let mut rows = database.entity_scan(transaction, &start, &end, limit + 1)?;
    let has_more = rows.len() > limit;
    rows.truncate(limit);
    let records = rows
        .into_iter()
        .map(|(_, raw)| {
            let indexed = decode_attempt_timing_index(&raw)?;
            if indexed.get("conversationId").and_then(Value::as_str) != Some(conversation_id) {
                return Err(invalid_data(
                    "timing trace conversation index escaped its prefix",
                ));
            }
            Ok(json!({
                "attempt_id": indexed["attemptId"],
                "task_id": indexed["taskId"],
                "status": indexed["status"],
                "turn_id": indexed["turnId"],
                "created_at": indexed["createdAt"],
                "settled_at": indexed.get("settledAt").cloned().unwrap_or(Value::Null),
            }))
        })
        .collect::<io::Result<Vec<_>>>()?;
    encode_response(&json!({"records": records, "has_more": has_more}))
}

fn lane_tail_turn_ids(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    ordinal: u64,
) -> io::Result<Vec<String>> {
    let prefix = lane_prefix(conversation_id, lane_id)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    let target = lane_index_key(transaction, conversation_id, lane_id, ordinal)?;
    let mut start = after_key(&target, transaction, TURN_LANE_INDEX_NAMESPACE)?;
    let mut turn_ids = Vec::new();
    while turn_ids.len() <= MAX_DELETE_ROWS {
        let page_limit = (MAX_DELETE_ROWS + 1 - turn_ids.len()).min(INDEX_PAGE_ROWS);
        let rows = database.entity_scan(transaction, &start, &end, page_limit)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().expect("nonempty lane tail page").0,
            transaction,
            TURN_LANE_INDEX_NAMESPACE,
        )?;
        for (_, stored) in rows {
            let turn = materialize_turn(database, transaction, &stored)?;
            turn_ids.push(
                turn.get("turnId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("lane tail Turn identity is malformed"))?
                    .to_owned(),
            );
        }
        if row_count < page_limit {
            break;
        }
        start = continuation;
    }
    if turn_ids.len() > MAX_DELETE_ROWS {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "regenerate lane tail exceeds 2000 turns",
        ));
    }
    Ok(turn_ids)
}

fn tombstones_at(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    deleted_at_ms: u64,
) -> io::Result<Vec<String>> {
    let mut prefix = conversation_prefix(conversation_id)?;
    prefix.extend_from_slice(&deleted_at_ms.to_be_bytes());
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_TOMBSTONE_NAMESPACE,
        &prefix,
    )?;
    let mut cursor = start;
    let mut rows = Vec::new();
    while rows.len() <= MAX_DELETE_ROWS {
        let limit = (MAX_DELETE_ROWS + 1 - rows.len()).min(INDEX_PAGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, limit)?;
        if page.is_empty() {
            break;
        }
        let page_len = page.len();
        cursor = after_key(
            &page.last().expect("nonempty tombstone page").0,
            transaction,
            TURN_TOMBSTONE_NAMESPACE,
        )?;
        rows.extend(page);
        if page_len < limit {
            break;
        }
    }
    if rows.len() > MAX_DELETE_ROWS {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "attempt replay tombstones exceed 2000 rows",
        ));
    }
    rows.into_iter()
        .map(|(key, _)| decode_tombstone_turn_id(&key, conversation_prefix(conversation_id)?.len()))
        .collect()
}

pub(crate) fn attempt_create(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AttemptCreateRequest,
) -> io::Result<Vec<u8>> {
    let command_key =
        attempt_command_key(transaction, &request.conversation_id, &request.command_id)?;
    if let Some(existing_attempt_id) = database.entity_get(transaction, &command_key)? {
        let existing_attempt_id = std::str::from_utf8(&existing_attempt_id)
            .map_err(|_| invalid_data("attempt command index is not UTF-8"))?;
        let attempt = attempt_get(database, transaction, existing_attempt_id)?
            .ok_or_else(|| invalid_data("attempt command index target is missing"))?;
        let attempt: Value = serde_json::from_slice(&attempt)
            .map_err(|_| invalid_data("attempt replay response is malformed"))?;
        let turn_id = attempt
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt replay Turn identity is malformed"))?;
        let turn = get(database, transaction, &request.conversation_id, turn_id)?
            .ok_or_else(|| invalid_data("attempt replay Turn is missing"))?;
        let turn: Value = serde_json::from_slice(&turn)
            .map_err(|_| invalid_data("attempt replay Turn is malformed"))?;
        let revision =
            crate::conversation_header::revision(database, transaction, &request.conversation_id)?;
        let needs_start = attempt.get("status").and_then(Value::as_str) == Some("pending")
            && attempt.get("taskId").and_then(Value::as_str) == Some("");
        let attempt_created_at = attempt
            .get("createdAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("attempt replay creation timestamp is malformed"))?;
        let mut response = Map::from_iter([
            ("turn".to_owned(), turn),
            ("attempt".to_owned(), attempt),
            ("conversationRevision".to_owned(), Value::from(revision)),
            ("streamCursor".to_owned(), Value::from(1)),
            ("idempotentReplay".to_owned(), Value::Bool(true)),
            ("_needsStart".to_owned(), Value::Bool(needs_start)),
        ]);
        if request.operation == "regenerate" {
            response.insert(
                "deletedTurnIds".to_owned(),
                Value::Array(
                    tombstones_at(
                        database,
                        transaction,
                        &request.conversation_id,
                        attempt_created_at,
                    )?
                    .into_iter()
                    .map(Value::String)
                    .collect(),
                ),
            );
        }
        return encode_response(&Value::Object(response));
    }

    let turn_key_value = turn_key(transaction, &request.conversation_id, &request.turn_id)?;
    let stored = database
        .entity_get(transaction, &turn_key_value)?
        .ok_or_else(|| not_found("turn not found"))?;
    let execution_epoch = crate::conversation_header::execution_epoch(
        database,
        transaction,
        &request.conversation_id,
    )?;
    let raw_turn = materialize_turn(database, transaction, &stored)?;
    let public = public_turn(raw_turn.clone(), execution_epoch)?;
    let current_revision = public
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    if current_revision != request.expected_projection_revision {
        return Err(typed_conflict(TurnConflictKind::ProjectionStale));
    }
    let current_attempt_id = public
        .get("currentAttemptId")
        .and_then(Value::as_str)
        .map(str::to_owned);
    let current_attempt = match current_attempt_id.as_deref() {
        None => None,
        Some(attempt_id) => attempt_get(database, transaction, attempt_id)?
            .map(|encoded| {
                serde_json::from_slice::<Value>(&encoded)
                    .map_err(|_| invalid_data("current attempt is malformed"))
            })
            .transpose()?,
    };
    if current_attempt.as_ref().is_some_and(|attempt| {
        matches!(
            attempt.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        )
    }) {
        return Err(conflict("this turn already has a live attempt"));
    }
    let settlement = public
        .get("settlement")
        .and_then(Value::as_object)
        .ok_or_else(|| invalid_data("invalid stored turn settlement"))?;
    let anchors = resume_option_anchors(settlement)?;
    let anchor = if request.operation == "regenerate" {
        Map::new()
    } else {
        anchors.get(&request.operation).cloned().ok_or_else(|| {
            conflict("attempt operation is not available for the current settlement")
        })?
    };
    if request
        .resume_anchor
        .as_ref()
        .is_some_and(|requested| requested != &anchor)
    {
        return Err(conflict("the requested resume anchor is not current"));
    }

    let mut projection = public
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .ok_or_else(|| invalid_data("turn projection is malformed"))?;
    let count_key =
        attempt_turn_count_key(transaction, &request.conversation_id, &request.turn_id)?;
    let stored_count = database.entity_get(transaction, &count_key)?;
    let attempt_count = if stored_count.is_none() && current_attempt_id.is_some() {
        1
    } else {
        decode_u64(stored_count, "turn attempt count is malformed")?
    };
    if attempt_count >= MAX_ATTEMPTS_PER_TURN as u64 {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn attempt history exceeds 64 entries",
        ));
    }
    let mut rewound_thinking = false;
    if request.operation != "regenerate" {
        if attempt_count <= 1 {
            if let (Some(attempt_id), Some(current)) =
                (current_attempt_id.as_deref(), current_attempt.as_ref())
            {
                projection_history_with_execution_identity(
                    &mut projection,
                    attempt_id,
                    current.get("taskId").and_then(Value::as_str).unwrap_or(""),
                );
            }
        }
        let lane_continues = matches!(request.operation.as_str(), "continue" | "answer_guidance")
            && projection.get("content").is_some_and(json_truthy);
        let content_tail = projection
            .get("content")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned);
        let thinking_tail = projection
            .get("thinking")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned);
        let rolled_content = (request.operation == "checkpoint_resume")
            .then_some(content_tail)
            .flatten();
        let rolled_thinking = (!lane_continues).then_some(thinking_tail).flatten();
        rewound_thinking = rolled_thinking.is_some();
        if rolled_content.is_some() || rolled_thinking.is_some() {
            let mut rolled = Map::from_iter([
                (
                    "blockId".to_owned(),
                    Value::String(format!(
                        "rolled-back:{}",
                        current_attempt_id.as_deref().unwrap_or(&request.turn_id)
                    )),
                ),
                ("at".to_owned(), Value::from(request.created_at_ms)),
            ]);
            if let Some(content) = rolled_content {
                rolled.insert("content".to_owned(), Value::String(content));
            }
            if let Some(thinking) = rolled_thinking {
                rolled.insert("thinking".to_owned(), Value::String(thinking));
            }
            if let Some(attempt_id) = current_attempt_id.as_deref() {
                rolled.insert("attemptId".to_owned(), Value::String(attempt_id.to_owned()));
            }
            let mut history = projection
                .get("rolledBack")
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .filter(|item| item.is_object())
                        .cloned()
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            history.push(Value::Object(rolled));
            if history.len() > 4 {
                history.drain(..history.len() - 4);
            }
            projection.insert("rolledBack".to_owned(), Value::Array(history));
        }
    }

    let mut submitted = None;
    let mut submitted_update = None;
    if let Some(input_update) = &request.input_update {
        let parent_turn_id = raw_turn
            .get("parentTurnId")
            .and_then(Value::as_str)
            .ok_or_else(|| conflict("generated turn has no editable submitted parent"))?;
        let parent_key = turn_key(transaction, &request.conversation_id, parent_turn_id)?;
        let parent_stored = database
            .entity_get(transaction, &parent_key)?
            .ok_or_else(|| conflict("generated turn has no editable submitted parent"))?;
        let parent = materialize_turn(database, transaction, &parent_stored)?;
        if !matches!(
            parent.get("actor").and_then(Value::as_str),
            Some("human" | "virtual_user" | "critic")
        ) || parent.get("projectionRevision").and_then(Value::as_u64)
            != request.expected_input_projection_revision
        {
            return Err(conflict("submitted turn changed since editing began"));
        }
        let update = ProjectionUpdateRequest {
            conversation_id: request.conversation_id.clone(),
            turn_id: parent_turn_id.to_owned(),
            projection_json: serde_json::to_vec(input_update)
                .map_err(|_| invalid_input("submitted projection cannot be encoded"))?,
            expected_projection_revision: request
                .expected_input_projection_revision
                .expect("matching revision is present"),
            updated_at_ms: request.created_at_ms,
            committed_at_ms: request.committed_at_ms,
        };
        let staged = stage_projection_update(
            database,
            transaction,
            &update,
            ProjectionUpdateMode::InternalSettledMutation,
        )?;
        submitted = Some(staged.turn.clone());
        submitted_update = Some((update, staged));
    }

    let mut deleted_turn_ids = Vec::new();
    let mut deleted_main_turns = 0;
    if request.operation == "regenerate" {
        projection = Map::from_iter([
            ("content".to_owned(), Value::String(String::new())),
            ("thinking".to_owned(), Value::String(String::new())),
            ("segments".to_owned(), Value::Array(Vec::new())),
            ("toolRounds".to_owned(), Value::Array(Vec::new())),
        ]);
        let lane_id = raw_turn
            .get("laneId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("turn lane identity is malformed"))?;
        let ordinal = raw_turn
            .get("ordinal")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
        let tail = lane_tail_turn_ids(
            database,
            transaction,
            &request.conversation_id,
            lane_id,
            ordinal,
        )?;
        if !tail.is_empty() {
            let rows = collect_deletion_closure(
                database,
                transaction,
                &request.conversation_id,
                &tail,
                &[],
                None,
            )?;
            (deleted_turn_ids, deleted_main_turns) = apply_deletion_rows(
                database,
                transaction,
                &request.conversation_id,
                &rows,
                request.created_at_ms,
            )?;
        }
    } else if request.operation == "checkpoint_resume" {
        if !matches!(anchor.get("content"), None | Some(Value::String(_)))
            || !matches!(anchor.get("thinking"), None | Some(Value::String(_)))
        {
            return Err(invalid_data("invalid checkpoint projection anchor"));
        }
        let segments = match anchor.get("segments") {
            None => Vec::new(),
            Some(Value::Array(segments)) => segments.clone(),
            Some(_) => return Err(invalid_data("invalid checkpoint projection anchor")),
        };
        let kept = match anchor.get("keptToolRounds") {
            None => 0,
            Some(Value::Number(value)) => value
                .as_u64()
                .and_then(|value| usize::try_from(value).ok())
                .ok_or_else(|| invalid_data("invalid checkpoint tool-round boundary"))?,
            Some(_) => return Err(invalid_data("invalid checkpoint tool-round boundary")),
        };
        let rounds = match projection.get("toolRounds") {
            None => Vec::new(),
            Some(Value::Array(rounds)) => rounds.clone(),
            Some(_) => return Err(invalid_data("invalid checkpoint tool-round projection")),
        };
        if kept > rounds.len() {
            return Err(invalid_data(
                "checkpoint tool-round boundary exceeds the projection",
            ));
        }
        let retained = match anchor.get("retainedToolRoundPositions") {
            None => rounds[..kept].to_vec(),
            Some(Value::Array(positions)) => {
                let mut previous = None;
                let mut selected = Vec::new();
                for position in positions {
                    let position = position
                        .as_u64()
                        .and_then(|value| usize::try_from(value).ok())
                        .ok_or_else(|| invalid_data("invalid checkpoint tool-round position"))?;
                    if position >= kept
                        || position >= rounds.len()
                        || previous.is_some_and(|previous| position <= previous)
                    {
                        return Err(invalid_data("invalid checkpoint tool-round position"));
                    }
                    selected.push(rounds[position].clone());
                    previous = Some(position);
                }
                let replayable = match anchor.get("replayableToolRounds") {
                    None => 0,
                    Some(Value::Number(value)) => value
                        .as_u64()
                        .and_then(|value| usize::try_from(value).ok())
                        .ok_or_else(|| {
                            invalid_data("checkpoint replayable tool-round count is inconsistent")
                        })?,
                    Some(_) => {
                        return Err(invalid_data(
                            "checkpoint replayable tool-round count is inconsistent",
                        ));
                    }
                };
                if replayable > selected.len() {
                    return Err(invalid_data(
                        "checkpoint replayable tool-round count is inconsistent",
                    ));
                }
                selected
            }
            Some(_) => return Err(invalid_data("invalid checkpoint tool-round positions")),
        };
        projection.insert("content".to_owned(), Value::String(String::new()));
        projection.insert("thinking".to_owned(), Value::String(String::new()));
        projection.insert("toolRounds".to_owned(), Value::Array(retained));
        projection.insert("segments".to_owned(), Value::Array(segments));
    } else if matches!(request.operation.as_str(), "continue" | "answer_guidance")
        && rewound_thinking
    {
        projection.insert("thinking".to_owned(), Value::String(String::new()));
    }

    let next_actor = request
        .target_actor
        .as_deref()
        .unwrap_or_else(|| raw_turn.get("actor").and_then(Value::as_str).unwrap_or(""));
    let next_kind = request
        .target_kind
        .as_deref()
        .unwrap_or_else(|| raw_turn.get("kind").and_then(Value::as_str).unwrap_or(""));
    if next_actor.is_empty() || next_kind.is_empty() {
        return Err(invalid_data("turn actor or kind is malformed"));
    }
    let attempt_document = Map::from_iter([
        (
            "attemptId".to_owned(),
            Value::String(request.attempt_id.clone()),
        ),
        (
            "conversationId".to_owned(),
            Value::String(request.conversation_id.clone()),
        ),
        ("turnId".to_owned(), Value::String(request.turn_id.clone())),
        (
            "commandId".to_owned(),
            Value::String(request.command_id.clone()),
        ),
        ("taskId".to_owned(), Value::String(String::new())),
        (
            "operation".to_owned(),
            Value::String(request.operation.clone()),
        ),
        ("status".to_owned(), Value::String("pending".to_owned())),
        (
            "baseProjectionRevision".to_owned(),
            Value::from(current_revision),
        ),
        ("resumeAnchor".to_owned(), Value::Object(anchor.clone())),
        ("createdAt".to_owned(), Value::from(request.created_at_ms)),
        ("startedAt".to_owned(), Value::Null),
        ("settledAt".to_owned(), Value::Null),
        (
            "_dispatchMode".to_owned(),
            Value::String(request.dispatch_mode.clone()),
        ),
        ("_queueId".to_owned(), Value::String(String::new())),
        ("_queueState".to_owned(), Value::String(String::new())),
        ("_config".to_owned(), request.config.clone()),
        ("_error".to_owned(), Value::Object(Map::new())),
    ]);
    let attempt_claim =
        global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, &request.attempt_id)?;
    if database.entity_get(transaction, &attempt_claim)?.is_some() {
        return Err(conflict("attempt identity already exists"));
    }
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: attempt_key(transaction, &request.conversation_id, &request.attempt_id)?,
            namespace: "generation_attempts".to_owned(),
            logical_key: request.attempt_id.clone(),
            value_json: serde_json::to_vec(&Value::Object(attempt_document.clone()))
                .map_err(|_| invalid_input("attempt cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: request.committed_at_ms,
        },
    )?;
    append_attempt_turn_directory(
        database,
        transaction,
        &request.conversation_id,
        &request.turn_id,
        current_attempt_id.as_deref(),
        &attempt_document,
    )?;
    database.entity_put(
        transaction,
        attempt_claim,
        encode_attempt_locator(transaction.owner_user_id(), &request.conversation_id)?,
    )?;
    if request.dispatch_mode == "conversation_executor" {
        database.entity_put(
            transaction,
            dispatchable_index_key(transaction, request.created_at_ms, &request.attempt_id)?,
            dispatchable_index_value(
                transaction.owner_user_id(),
                &request.conversation_id,
                &request.turn_id,
                &request.attempt_id,
            )?,
        )?;
    }
    database.entity_put(
        transaction,
        recovery_index_key(transaction, request.created_at_ms, &request.attempt_id)?,
        recovery_index_value(
            &attempt_document,
            serde_json::to_vec(&projection)
                .map_err(|_| invalid_data("turn projection cannot be encoded"))?
                .len(),
        )?,
    )?;
    let lane_id = raw_turn
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn lane identity is malformed"))?;
    set_lane_live_attempt(
        database,
        transaction,
        &request.conversation_id,
        lane_id,
        &request.turn_id,
        &request.attempt_id,
        true,
    )?;
    database.entity_put(
        transaction,
        command_key,
        request.attempt_id.as_bytes().to_vec(),
    )?;
    database.entity_put(
        transaction,
        count_key,
        attempt_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("turn attempt count overflow"))?
            .to_le_bytes()
            .to_vec(),
    )?;
    let staged_turn = stage_attempt_turn_transition(
        database,
        transaction,
        &stored,
        AttemptTurnTransition {
            conversation_id: &request.conversation_id,
            turn_id: &request.turn_id,
            attempt_id: &request.attempt_id,
            actor: next_actor,
            kind: next_kind,
            projection,
            updated_at_ms: request.created_at_ms,
            committed_at_ms: request.committed_at_ms,
        },
    )?;
    let revision = if deleted_main_turns == 0 {
        crate::conversation_header::advance_for_turn(
            database,
            transaction,
            &request.conversation_id,
            request.created_at_ms,
            request.committed_at_ms,
            false,
        )?
    } else {
        crate::conversation_header::advance_for_turn_delete(
            database,
            transaction,
            &request.conversation_id,
            deleted_main_turns,
            request.created_at_ms,
            request.committed_at_ms,
        )?
    };
    if let Some((update, staged)) = &submitted_update {
        store_projection_sync_event(
            database,
            transaction,
            update,
            &staged.before,
            &staged.after,
            revision,
            &[],
        )?;
    }
    if !deleted_turn_ids.is_empty() {
        store_delete_sync_event(
            database,
            transaction,
            &request.conversation_id,
            &deleted_turn_ids,
            &[],
            revision,
            request.created_at_ms,
        )?;
    }
    let public_attempt = public_attempt(&attempt_document)?;
    append_attempt_event(
        database,
        transaction,
        AttemptEventAppend {
            conversation_id: &request.conversation_id,
            turn_id: &request.turn_id,
            attempt_id: &request.attempt_id,
            projection_revision: current_revision
                .checked_add(1)
                .ok_or_else(|| invalid_data("turn projection revision overflow"))?,
            event_type: "status_changed",
            payload: json!({
                "status": "pending",
                "operation": request.operation,
                "projectionPatch": projection_patch(
                    &staged_turn.before,
                    &staged_turn.after,
                    current_revision,
                )?,
                "turnState": {
                    "turnId": request.turn_id,
                    "status": "pending",
                    "actor": next_actor,
                    "kind": next_kind,
                    "currentAttemptId": request.attempt_id,
                    "settlement": {},
                    "updatedAt": request.created_at_ms,
                },
                "attempts": [public_attempt.clone()],
            }),
            occurred_at_ms: request.created_at_ms,
            publish_conversation_sync: true,
        },
    )?;
    let mut response = Map::from_iter([
        ("turn".to_owned(), staged_turn.turn),
        ("attempt".to_owned(), public_attempt),
        ("conversationRevision".to_owned(), Value::from(revision)),
        ("streamCursor".to_owned(), Value::from(1)),
        ("idempotentReplay".to_owned(), Value::Bool(false)),
        ("_needsStart".to_owned(), Value::Bool(true)),
    ]);
    if let Some(submitted) = submitted {
        response.insert("submittedTurn".to_owned(), submitted);
    }
    if request.operation == "regenerate" {
        response.insert(
            "deletedTurnIds".to_owned(),
            Value::Array(deleted_turn_ids.into_iter().map(Value::String).collect()),
        );
    }
    encode_response(&Value::Object(response))
}

pub(crate) fn attempt_claim(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    dispatch_owner_id: &str,
    committed_at_ms: u64,
) -> io::Result<Vec<u8>> {
    let claim_key = global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)?;
    let Some(locator) = database.entity_get(transaction, &claim_key)? else {
        return encode_response(&Value::Bool(false));
    };
    let (key, located_conversation_id) = match decode_attempt_locator(&locator)? {
        AttemptLocator::LegacyOwner(owner_user_id) => {
            if owner_user_id != transaction.owner_user_id() {
                return encode_response(&Value::Bool(false));
            }
            (legacy_attempt_key(transaction, attempt_id)?, None)
        }
        AttemptLocator::Conversation {
            owner_user_id,
            conversation_id,
        } => {
            if owner_user_id != transaction.owner_user_id() {
                return encode_response(&Value::Bool(false));
            }
            if crate::conversation_header::sync_header(database, transaction, &conversation_id)?
                .is_none()
            {
                return encode_response(&Value::Bool(false));
            }
            (
                attempt_key(transaction, &conversation_id, attempt_id)?,
                Some(conversation_id),
            )
        }
    };
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return encode_response(&Value::Bool(false));
    };
    let physical_version = crate::versioned_document::stored_document_version(
        &stored,
        "generation_attempts",
        attempt_id,
    )?;
    let (_, document_json) = crate::versioned_document::materialize_stored_document(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        "generation_attempts",
    )?;
    let mut attempt = serde_json::from_slice::<Value>(&document_json)
        .ok()
        .and_then(|value| value.as_object().cloned())
        .ok_or_else(|| invalid_data("attempt document is malformed"))?;
    let conversation_id = attempt
        .get("conversationId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt conversation identity is malformed"))?;
    if located_conversation_id
        .as_deref()
        .is_some_and(|located| located != conversation_id)
    {
        return Err(invalid_data(
            "attempt locator conversation identity differs",
        ));
    }
    if located_conversation_id.is_none()
        && crate::conversation_header::sync_header(database, transaction, conversation_id)?
            .is_none()
    {
        return encode_response(&Value::Bool(false));
    }
    if located_conversation_id.is_some() {
        let turn_id = attempt
            .get("turnId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?;
        if database
            .entity_get(
                transaction,
                &turn_key(transaction, conversation_id, turn_id)?,
            )?
            .is_none()
        {
            return encode_response(&Value::Bool(false));
        }
    }
    let status = attempt
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt status is malformed"))?;
    let queue_state = match attempt
        .get("_queueState")
        .or_else(|| attempt.get("queueState"))
    {
        None => "",
        Some(Value::String(value)) => value,
        Some(_) => return Err(invalid_data("attempt queue state is malformed")),
    };
    if status != "pending" || !queue_state.is_empty() {
        return encode_response(&Value::Bool(false));
    }
    let legacy_claim = format!("@dispatching:{attempt_id}");
    let dispatch_claim = if dispatch_owner_id.is_empty() {
        legacy_claim.clone()
    } else {
        format!("{legacy_claim}:{dispatch_owner_id}")
    };
    let existing_task_id = attempt
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt task identity is malformed"))?;
    if existing_task_id == dispatch_claim {
        return encode_response(&Value::Bool(!dispatch_owner_id.is_empty()));
    }
    if !existing_task_id.is_empty() {
        return encode_response(&Value::Bool(false));
    }
    remove_dispatchable_index(database, transaction, &attempt)?;
    let previous_attempt = attempt.clone();
    attempt.insert("taskId".to_owned(), Value::String(dispatch_claim));
    update_attempt_timing_indexes(database, transaction, &previous_attempt, &attempt)?;
    update_attempt_turn_directory(database, transaction, &attempt)?;
    update_recovery_index(database, transaction, &previous_attempt, &attempt)?;
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key,
            namespace: "generation_attempts".to_owned(),
            logical_key: attempt_id.to_owned(),
            value_json: serde_json::to_vec(&Value::Object(attempt))
                .map_err(|_| invalid_data("attempt cannot be encoded"))?,
            expected_version: Some(physical_version),
            updated_at_ms: committed_at_ms,
        },
    )?;
    encode_response(&Value::Bool(true))
}

pub(crate) fn attempt_bind(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    task_id: &str,
    dispatch_owner_id: &str,
    now_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? else {
        return encode_response(&Value::Null);
    };
    let status = loaded
        .document
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt status is malformed"))?
        .to_owned();
    if !matches!(status.as_str(), "pending" | "running") {
        return encode_response(&Value::Null);
    }
    let Some(turn) = load_current_turn_for_attempt(
        database,
        transaction,
        attempt_id,
        &loaded.document,
        &loaded.conversation_id,
    )?
    else {
        return encode_response(&Value::Null);
    };
    let existing_task_id = loaded
        .document
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt task identity is malformed"))?;
    let legacy_claim = format!("@dispatching:{attempt_id}");
    let dispatch_claim = if dispatch_owner_id.is_empty() {
        legacy_claim.clone()
    } else {
        format!("{legacy_claim}:{dispatch_owner_id}")
    };
    if !existing_task_id.is_empty()
        && existing_task_id != legacy_claim
        && existing_task_id != dispatch_claim
        && existing_task_id != task_id
    {
        return Err(conflict("generation attempt is already bound to a task"));
    }
    if existing_task_id == task_id {
        return encode_response(&public_attempt(&loaded.document)?);
    }
    remove_dispatchable_index(database, transaction, &loaded.document)?;
    let mut attempt = loaded.document.clone();
    attempt.insert("taskId".to_owned(), Value::String(task_id.to_owned()));
    store_attempt_document(database, transaction, attempt_id, &loaded, &attempt, now_ms)?;
    if status == "pending" {
        let (turn_id, projection, previous_revision, next_revision) =
            update_attempt_turn_lifecycle(
                database,
                transaction,
                &turn,
                &loaded.conversation_id,
                None,
                now_ms,
                now_ms,
            )?;
        crate::conversation_header::advance_for_turn(
            database,
            transaction,
            &loaded.conversation_id,
            now_ms,
            now_ms,
            false,
        )?;
        append_attempt_event(
            database,
            transaction,
            AttemptEventAppend {
                conversation_id: &loaded.conversation_id,
                turn_id: &turn_id,
                attempt_id,
                projection_revision: next_revision,
                event_type: "status_changed",
                payload: json!({
                "status": "pending",
                "dispatchState": "queued",
                "attempts": [public_attempt(&attempt)?],
                "projectionPatch": projection_patch(
                    &projection,
                    &projection,
                    previous_revision,
                )?
                }),
                occurred_at_ms: now_ms,
                publish_conversation_sync: true,
            },
        )?;
    }
    encode_response(&public_attempt(&attempt)?)
}

pub(crate) fn attempt_start(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    task_id: &str,
    now_ms: u64,
) -> io::Result<Vec<u8>> {
    let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? else {
        return encode_response(&Value::Null);
    };
    if loaded.document.get("taskId").and_then(Value::as_str) != Some(task_id) {
        return encode_response(&Value::Null);
    }
    let Some(turn) = load_current_turn_for_attempt(
        database,
        transaction,
        attempt_id,
        &loaded.document,
        &loaded.conversation_id,
    )?
    else {
        return encode_response(&Value::Null);
    };
    let status = loaded
        .document
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt status is malformed"))?;
    if status == "running" {
        return encode_response(&public_attempt(&loaded.document)?);
    }
    if status != "pending" || turn.document.get("status").and_then(Value::as_str) != Some("pending")
    {
        return encode_response(&Value::Null);
    }
    let mut attempt = loaded.document.clone();
    attempt.insert("status".to_owned(), Value::String("running".to_owned()));
    if attempt.get("startedAt").is_none_or(Value::is_null) {
        attempt.insert("startedAt".to_owned(), Value::from(now_ms));
    }
    store_attempt_document(database, transaction, attempt_id, &loaded, &attempt, now_ms)?;
    let (turn_id, projection, previous_revision, next_revision) = update_attempt_turn_lifecycle(
        database,
        transaction,
        &turn,
        &loaded.conversation_id,
        Some("running"),
        now_ms,
        now_ms,
    )?;
    crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &loaded.conversation_id,
        now_ms,
        now_ms,
        false,
    )?;
    append_attempt_event(
        database,
        transaction,
        AttemptEventAppend {
            conversation_id: &loaded.conversation_id,
            turn_id: &turn_id,
            attempt_id,
            projection_revision: next_revision,
            event_type: "status_changed",
            payload: json!({
            "status": "running",
            "dispatchState": "running",
            "attempts": [public_attempt(&attempt)?],
            "projectionPatch": projection_patch(
                &projection,
                &projection,
                previous_revision,
            )?
            }),
            occurred_at_ms: now_ms,
            publish_conversation_sync: true,
        },
    )?;
    encode_response(&public_attempt(&attempt)?)
}

fn merge_terminal_client_trace(
    projection: &mut Map<String, Value>,
    attempt: &Map<String, Value>,
    task_id: &str,
) -> io::Result<()> {
    let mut merged = projection
        .get("timingTrace")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let receipt_trace = attempt
        .get("_timingTrace")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();
    let receipt_task_id = receipt_trace
        .get("taskId")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if receipt_task_id.is_empty() || receipt_task_id == task_id {
        if let Some(observations) = receipt_trace.get("clientObservations") {
            merged.insert(
                "clientObservations".to_owned(),
                observations
                    .as_array()
                    .cloned()
                    .map_or_else(|| Value::Array(Vec::new()), Value::Array),
            );
            match receipt_trace
                .get("clientObservationDroppedCount")
                .and_then(Value::as_u64)
            {
                Some(count) if count > 0 => {
                    merged.insert(
                        "clientObservationDroppedCount".to_owned(),
                        Value::from(count),
                    );
                }
                _ => {
                    merged.remove("clientObservationDroppedCount");
                }
            }
        }
    }
    if merged.is_empty() {
        projection.remove("timingTrace");
    } else {
        merged.insert("version".to_owned(), Value::from(1));
        merged.insert(
            "taskId".to_owned(),
            Value::String(task_id.chars().take(256).collect()),
        );
        projection.insert(
            "timingTrace".to_owned(),
            Value::Object(compact_timing_trace(&merged)?),
        );
    }
    Ok(())
}

pub(crate) fn event_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest,
) -> io::Result<Vec<u8>> {
    let Some(loaded_attempt) = load_attempt_for_update(database, transaction, &request.attempt_id)?
    else {
        return encode_response(&json!({"applied": false}));
    };
    let attempt_status = loaded_attempt
        .document
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt status is malformed"))?;
    if !matches!(attempt_status, "pending" | "running") {
        return encode_response(&json!({"applied": false}));
    }
    let Some(loaded_turn) = load_current_turn_for_attempt(
        database,
        transaction,
        &request.attempt_id,
        &loaded_attempt.document,
        &loaded_attempt.conversation_id,
    )?
    else {
        return encode_response(&json!({"applied": false}));
    };
    let stored_task_id = loaded_attempt
        .document
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt task identity is malformed"))?;
    let starts_attempt = !request.terminal && attempt_status == "pending";
    if starts_attempt && (stored_task_id.is_empty() || stored_task_id != request.task_id) {
        return encode_response(&json!({"applied": false}));
    }

    let mut turn = loaded_turn.document.clone();
    let turn_id = turn
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn identity is malformed"))?
        .to_owned();
    let lane_id = turn
        .get("laneId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn lane identity is malformed"))?
        .to_owned();
    let ordinal = turn
        .get("ordinal")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
    let previous_updated_at_ms = turn
        .get("updatedAt")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn update timestamp is malformed"))?;
    let previous_revision = turn
        .get("projectionRevision")
        .and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let next_revision = previous_revision
        .checked_add(1)
        .ok_or_else(|| invalid_data("turn projection revision overflow"))?;
    let previous_projection = turn
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    let (mut next_projection, mut replay_patch) = if request.slim {
        if request.projection_patch.is_some() {
            return Err(invalid_input(
                "slim Turn event cannot carry a projection patch",
            ));
        }
        let mut next = previous_projection.clone();
        next.insert("content".to_owned(), Value::String(request.content.clone()));
        next.insert(
            "thinking".to_owned(),
            Value::String(request.thinking.clone()),
        );
        let patch = crate::turn_projection_patch::build_projection_patch(
            &previous_projection,
            &next,
            previous_revision,
            next_revision,
        )
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::OutOfMemory,
                "Turn event patch exceeds its bound",
            )
        })?;
        (next, patch)
    } else if let Some(patch) = &request.projection_patch {
        if patch.get("baseRevision").and_then(Value::as_u64) != Some(previous_revision) {
            return Err(typed_conflict(TurnConflictKind::ProjectionStale));
        }
        if patch.get("targetRevision").and_then(Value::as_u64) != Some(next_revision) {
            return Err(invalid_input(
                "Turn projection patch must advance exactly one revision",
            ));
        }
        let next =
            crate::turn_projection_patch::apply_projection_patch(Some(&previous_projection), patch)
                .map_err(|_| invalid_input("Turn projection patch is invalid"))?;
        (next, patch.clone())
    } else {
        let next = request.projection.as_object().cloned().unwrap_or_default();
        let patch = crate::turn_projection_patch::build_projection_patch(
            &previous_projection,
            &next,
            previous_revision,
            next_revision,
        )
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::OutOfMemory,
                "Turn event patch exceeds its bound",
            )
        })?;
        (next, patch)
    };

    let mut attempt = loaded_attempt.document.clone();
    if !request.terminal {
        let created_at_ms = attempt
            .get("createdAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
        let projection_bytes = serde_json::to_vec(&next_projection)
            .map_err(|_| invalid_data("turn projection cannot be encoded"))?
            .len();
        database.entity_put(
            transaction,
            recovery_index_key(transaction, created_at_ms, &request.attempt_id)?,
            recovery_index_value(&attempt, projection_bytes)?,
        )?;
    }
    if starts_attempt {
        attempt.insert("status".to_owned(), Value::String("running".to_owned()));
        if attempt.get("startedAt").is_none_or(Value::is_null) {
            attempt.insert("startedAt".to_owned(), Value::from(request.now_ms));
        }
    }
    if request.terminal {
        merge_terminal_client_trace(&mut next_projection, &attempt, stored_task_id)?;
        replay_patch = crate::turn_projection_patch::build_projection_patch(
            &previous_projection,
            &next_projection,
            previous_revision,
            next_revision,
        )
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::OutOfMemory,
                "terminal Turn event patch exceeds its bound",
            )
        })?;
        attempt.insert("status".to_owned(), Value::String(request.status.clone()));
        attempt.insert("error".to_owned(), Value::Object(request.error.clone()));
        attempt.insert(
            "_timingTrace".to_owned(),
            next_projection
                .get("timingTrace")
                .cloned()
                .unwrap_or_else(|| json!({})),
        );
        attempt.insert("settledAt".to_owned(), Value::from(request.now_ms));
    }
    if starts_attempt || request.terminal {
        store_attempt_document(
            database,
            transaction,
            &request.attempt_id,
            &loaded_attempt,
            &attempt,
            request.now_ms,
        )?;
    }

    let replay_patch_bytes = serde_json::to_vec(&replay_patch)
        .map_err(|_| invalid_data("Turn projection patch cannot be encoded"))?
        .len();
    let next_projection_head = if request.terminal {
        retire_projection_head(
            database,
            transaction,
            &loaded_attempt.conversation_id,
            &turn_id,
            loaded_turn.projection_head.as_ref(),
        )?;
        None
    } else if let Some(current) = &loaded_turn.projection_head {
        let next_count = current
            .patch_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("Turn projection patch count overflows"))?;
        let next_bytes = current
            .patch_bytes
            .checked_add(replay_patch_bytes)
            .ok_or_else(|| invalid_data("Turn projection patch bytes overflow"))?;
        if next_count <= MAX_TURN_PROJECTION_HEAD_PATCHES
            && next_bytes <= MAX_TURN_PROJECTION_PATCH_BYTES
        {
            let next_head = ProjectionHeadDescriptor {
                head_id: current.head_id.clone(),
                attempt_id: current.attempt_id.clone(),
                base_revision: current.base_revision,
                patch_count: next_count,
                patch_bytes: next_bytes,
            };
            store_projection_head_patch(
                database,
                transaction,
                &loaded_attempt.conversation_id,
                &turn_id,
                &next_head,
                &replay_patch,
                request.now_ms,
            )?;
            Some(next_head)
        } else {
            retire_projection_head(
                database,
                transaction,
                &loaded_attempt.conversation_id,
                &turn_id,
                Some(current),
            )?;
            let checkpoint_head = ProjectionHeadDescriptor {
                head_id: projection_head_id(&request.attempt_id, next_revision),
                attempt_id: request.attempt_id.clone(),
                base_revision: next_revision,
                patch_count: 0,
                patch_bytes: 0,
            };
            store_projection_checkpoint(
                database,
                transaction,
                &loaded_attempt.conversation_id,
                &turn_id,
                &checkpoint_head,
                &next_projection,
                request.now_ms,
            )?;
            Some(checkpoint_head)
        }
    } else {
        let previous_projection_bytes = serde_json::to_vec(&previous_projection)
            .map_err(|_| invalid_data("Turn projection cannot be encoded"))?
            .len();
        let next_projection_bytes = serde_json::to_vec(&next_projection)
            .map_err(|_| invalid_data("Turn projection cannot be encoded"))?
            .len();
        if previous_projection_bytes > MAX_TURN_PROJECTION_INLINE_LIVE_BYTES
            || next_projection_bytes > MAX_TURN_PROJECTION_INLINE_LIVE_BYTES
        {
            let head = ProjectionHeadDescriptor {
                head_id: projection_head_id(&request.attempt_id, previous_revision),
                attempt_id: request.attempt_id.clone(),
                base_revision: previous_revision,
                patch_count: 1,
                patch_bytes: replay_patch_bytes,
            };
            store_projection_checkpoint(
                database,
                transaction,
                &loaded_attempt.conversation_id,
                &turn_id,
                &head,
                &previous_projection,
                request.now_ms,
            )?;
            store_projection_head_patch(
                database,
                transaction,
                &loaded_attempt.conversation_id,
                &turn_id,
                &head,
                &replay_patch,
                request.now_ms,
            )?;
            Some(head)
        } else {
            None
        }
    };
    if let Some(head) = &next_projection_head {
        turn.insert("projection".to_owned(), json!({}));
        turn.insert("_projectionHead".to_owned(), projection_head_value(head));
    } else {
        turn.insert(
            "projection".to_owned(),
            Value::Object(next_projection.clone()),
        );
        turn.remove("_projectionHead");
    }
    turn.insert("projectionRevision".to_owned(), Value::from(next_revision));
    turn.insert("status".to_owned(), Value::String(request.status.clone()));
    turn.insert(
        "settlement".to_owned(),
        Value::Object(request.settlement.clone()),
    );
    turn.insert("updatedAt".to_owned(), Value::from(request.now_ms));
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: loaded_turn.key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&loaded_attempt.conversation_id, &turn_id),
            value_json: serde_json::to_vec(&Value::Object(turn.clone()))
                .map_err(|_| invalid_data("turn cannot be encoded"))?,
            expected_version: Some(loaded_turn.physical_version),
            updated_at_ms: request.now_ms,
        },
    )?;
    let staged = database
        .entity_get(transaction, &loaded_turn.key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    database.entity_put(
        transaction,
        lane_index_key(
            transaction,
            &loaded_attempt.conversation_id,
            &lane_id,
            ordinal,
        )?,
        staged,
    )?;
    put_lane_compaction_index(database, transaction, &turn)?;
    database.entity_delete(
        transaction,
        updated_index_key(
            transaction,
            &loaded_attempt.conversation_id,
            previous_updated_at_ms,
            &turn_id,
        )?,
    )?;
    database.entity_put(
        transaction,
        updated_index_key(
            transaction,
            &loaded_attempt.conversation_id,
            request.now_ms,
            &turn_id,
        )?,
        encode_updated_index_value(&turn_id, next_revision)?,
    )?;
    if request.terminal {
        crate::search_dirty::mark(
            database,
            transaction,
            TURN_SEARCH_DIRTY_NAMESPACE,
            &loaded_attempt.conversation_id,
        )?;
    }

    crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &loaded_attempt.conversation_id,
        request.now_ms,
        request.now_ms,
        false,
    )?;
    // Legacy terminal writes always materialize the settled projection and
    // expose that encoded size as durable event evidence.  Live slim frames
    // retain the compact two-text-field measurement.
    let projection_evidence = if request.slim && !request.terminal {
        json!({
            "content": request.content,
            "thinking": request.thinking,
        })
    } else {
        Value::Object(next_projection.clone())
    };
    let projection_bytes = serde_json::to_vec(&projection_evidence)
        .map_err(|_| invalid_data("Turn projection cannot be encoded"))?
        .len();
    let mut event_payload = request.event_payload.clone();
    event_payload.remove("projection");
    event_payload.insert("projectionPatch".to_owned(), Value::Object(replay_patch));
    event_payload.insert("status".to_owned(), Value::String(request.status.clone()));
    event_payload.insert("projectionBytes".to_owned(), Value::from(projection_bytes));
    if starts_attempt {
        event_payload.insert(
            "attempts".to_owned(),
            Value::Array(vec![public_attempt(&attempt)?]),
        );
    }
    append_attempt_event(
        database,
        transaction,
        AttemptEventAppend {
            conversation_id: &loaded_attempt.conversation_id,
            turn_id: &turn_id,
            attempt_id: &request.attempt_id,
            projection_revision: next_revision,
            event_type: if request.terminal {
                "terminal_settlement"
            } else {
                &request.event_type
            },
            payload: Value::Object(event_payload),
            occurred_at_ms: request.now_ms,
            publish_conversation_sync: true,
        },
    )?;
    let task_event = request
        .task_event
        .as_ref()
        .map(|item| {
            crate::indexed_stream::append(
                database,
                transaction,
                &crate::indexed_stream::AppendRequest {
                    task_id: item.task_id.clone(),
                    application_sequence: item.application_sequence,
                    event_type: item.event_type.clone(),
                    payload_json: item.payload_json.clone(),
                    created_at_ms: item.created_at_ms,
                },
            )
        })
        .transpose()?
        .map(|bytes| {
            serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| invalid_data("carried task event response is malformed"))
        })
        .transpose()?;
    encode_response(&json!({
        "applied": true,
        "status": request.status,
        "projection_revision": next_revision,
        "task_event": task_event
    }))
}

fn recovery_round_is_superseded(round: &Value) -> bool {
    let Some(round) = round.as_object() else {
        return false;
    };
    if round
        .get("_providerAttemptDiscarded")
        .and_then(Value::as_bool)
        == Some(true)
    {
        return true;
    }
    let Some(first_result) = round
        .get("results")
        .and_then(Value::as_array)
        .and_then(|results| results.first())
        .and_then(Value::as_object)
    else {
        return false;
    };
    if first_result.get("badge").and_then(Value::as_str) != Some("superseded") {
        return false;
    }
    let fetched_characters = first_result
        .get("fetchedChars")
        .and_then(Value::as_f64)
        .is_some_and(|value| value > 0.0);
    round.get("toolContent").is_none_or(Value::is_null)
        && first_result.get("fetched").and_then(Value::as_bool) != Some(true)
        && !fetched_characters
}

fn recovery_caller_is_valid(value: Option<&Value>) -> bool {
    let Some(value) = value else {
        return true;
    };
    let Some(caller) = value.as_object() else {
        return false;
    };
    let identity = match caller.get("type").and_then(Value::as_str) {
        Some("program") => caller.get("caller_id"),
        Some("multi_agent") => caller.get("agent_name"),
        _ => return false,
    };
    identity
        .and_then(Value::as_str)
        .map(str::trim)
        .is_some_and(|identity| !identity.is_empty() && identity.chars().count() <= 512)
}

fn recovery_arguments_are_replayable(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) | Some(Value::Object(_)) => true,
        // The live sanitizer maps malformed and non-object provider strings
        // to `{}` while preserving their paired error result as the fact.
        Some(Value::String(_)) => true,
        _ => false,
    }
}

struct RecoveryReplayPrefix {
    replayable_rows: usize,
    blocked_position: Option<usize>,
    blocked_reason: &'static str,
    retained_positions: Vec<usize>,
}

fn recovery_replay_prefix(rounds: &[Value]) -> RecoveryReplayPrefix {
    let mut replayable_rows = 0_usize;
    let mut blocked_position = None;
    let mut blocked_reason = "";
    for (position, value) in rounds.iter().enumerate() {
        let Some(round) = value.as_object() else {
            continue;
        };
        if recovery_round_is_superseded(value) || !round.contains_key("toolCallId") {
            continue;
        }
        let tool_call_id = round.get("toolCallId").and_then(Value::as_str);
        let tool_name = round.get("toolName").and_then(Value::as_str);
        if tool_call_id.is_none_or(|value| value.is_empty() || value.chars().count() > 512) {
            blocked_position = Some(position);
            blocked_reason = "invalid_tool_call_id";
            break;
        }
        if tool_name.is_none_or(|value| value.is_empty() || value.chars().count() > 512) {
            blocked_position = Some(position);
            blocked_reason = "invalid_tool_name";
            break;
        }
        if !round.get("toolContent").is_some_and(Value::is_string) {
            blocked_position = Some(position);
            blocked_reason = if round.get("toolContent").is_none_or(Value::is_null) {
                "missing_tool_result"
            } else {
                "invalid_tool_result"
            };
            break;
        }
        if !recovery_caller_is_valid(round.get("caller")) {
            blocked_position = Some(position);
            blocked_reason = "invalid_tool_caller";
            break;
        }
        if !recovery_arguments_are_replayable(round.get("toolArgs")) {
            blocked_position = Some(position);
            blocked_reason = "invalid_tool_arguments";
            break;
        }
        replayable_rows += 1;
    }
    let boundary = blocked_position.unwrap_or(rounds.len());
    let retained_positions = (0..boundary)
        .filter(|position| !recovery_round_is_superseded(&rounds[*position]))
        .collect();
    RecoveryReplayPrefix {
        replayable_rows,
        blocked_position,
        blocked_reason,
        retained_positions,
    }
}

fn restart_settlement(attempt: &Map<String, Value>, projection: &Map<String, Value>) -> Value {
    let content = projection
        .get("content")
        .and_then(Value::as_str)
        .unwrap_or("");
    let thinking_is_present = projection.get("thinking").is_some_and(json_truthy);
    let rounds = projection
        .get("toolRounds")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    let prefix = recovery_replay_prefix(rounds);
    let model = attempt
        .get("_config")
        .and_then(Value::as_object)
        .and_then(|config| config.get("model"))
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    let supports_prefill = !model.is_empty()
        && !model.contains("claude")
        && !model.contains("anthropic")
        && !model.contains("fable");
    let mut options = Vec::new();
    if !content.is_empty() && supports_prefill {
        options.push(json!({
            "operation": "continue",
            "anchor": {"type": "lossless_prefill", "contentChars": content.chars().count()}
        }));
    } else if content.is_empty() && (prefix.replayable_rows > 0 || thinking_is_present) {
        options.push(json!({
            "operation": "continue",
            "anchor": {"type": "replay_only", "contentChars": 0}
        }));
    }
    if prefix.replayable_rows > 0 {
        options.push(json!({
            "operation": "checkpoint_resume",
            "anchor": {
                "type": "tool_checkpoint",
                "keptToolRounds": prefix.blocked_position.unwrap_or(rounds.len()),
                "replayableToolRounds": prefix.replayable_rows,
                "retainedToolRoundPositions": prefix.retained_positions.clone(),
                "content": "", "thinking": "", "segments": []
            }
        }));
    }
    if prefix.blocked_reason == "missing_tool_result" {
        if let Some((position, round)) = prefix
            .blocked_position
            .and_then(|position| rounds.get(position).map(|round| (position, round)))
            .and_then(|(position, round)| round.as_object().map(|round| (position, round)))
            .filter(|(_, round)| {
                round.get("toolName").and_then(Value::as_str) == Some("ask_human")
                    && round.get("status").and_then(Value::as_str) == Some("awaiting_human")
                    && round
                        .get("guidanceId")
                        .and_then(Value::as_str)
                        .is_some_and(|value| !value.is_empty())
            })
        {
            options.push(json!({
                "operation": "answer_guidance",
                "anchor": {
                    "type": "human_guidance",
                    "guidanceId": round.get("guidanceId").and_then(Value::as_str).unwrap_or(""),
                    "toolCallId": round.get("toolCallId").and_then(Value::as_str).unwrap_or(""),
                    "question": round.get("guidanceQuestion").and_then(Value::as_str).unwrap_or(""),
                    "responseType": round.get("guidanceType").and_then(Value::as_str).unwrap_or("free_text"),
                    "roundPosition": position,
                    "keptToolRounds": position,
                    "retainedToolRoundPositions": prefix.retained_positions,
                }
            }));
        }
    }
    options.push(json!({
        "operation": "regenerate",
        "anchor": {"type": "turn_start"}
    }));
    json!({
        "outcome": "interrupted",
        "cause": "server_restart",
        "evidence": "user_abort",
        "streamState": null,
        "providerFinishReason": "interrupted",
        "error": null,
        "resumeOptions": options,
    })
}

pub(crate) fn recover(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &RecoverRequest,
) -> io::Result<Vec<u8>> {
    let (start, namespace_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_RECOVERY_INDEX_NAMESPACE,
        b"",
    )?;
    let end = match request.created_before_ms {
        Some(cutoff) => EntityKey::new(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            ATTEMPT_RECOVERY_INDEX_NAMESPACE,
            &cutoff.to_be_bytes(),
        )?,
        None => namespace_end,
    };
    let mut cursor = start;
    let mut rows = Vec::new();
    while rows.len() <= MAX_RECOVERY_INDEX_ROWS_PER_OWNER {
        let limit = (MAX_RECOVERY_INDEX_ROWS_PER_OWNER + 1 - rows.len())
            .min(crate::generated_tofudb_ir::MAX_ENTITY_RANGE_ROWS);
        let page = database.entity_scan(transaction, &cursor, &end, limit)?;
        if page.is_empty() {
            break;
        }
        let page_len = page.len();
        cursor = after_key(
            &page.last().expect("nonempty recovery page").0,
            transaction,
            ATTEMPT_RECOVERY_INDEX_NAMESPACE,
        )?;
        rows.extend(page);
        if page_len < limit {
            break;
        }
    }
    if rows.len() > MAX_RECOVERY_INDEX_ROWS_PER_OWNER {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "live Turn recovery index exceeds 10000 rows",
        ));
    }

    let mut recovered = 0_usize;
    let mut remaining = 0_usize;
    let mut projection_bytes = 0_usize;
    for (index_key, encoded_locator) in rows {
        let locator = serde_json::from_slice::<Value>(&encoded_locator)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("attempt recovery index is malformed"))?;
        let text = |field: &str| {
            locator
                .get(field)
                .and_then(Value::as_str)
                .ok_or_else(|| invalid_data("attempt recovery identity is malformed"))
        };
        let attempt_id = text("attemptId")?.to_owned();
        let task_id = text("taskId")?;
        if request.exclude_task_ids.contains(task_id) {
            continue;
        }
        let indexed_projection_bytes = locator
            .get("projectionBytes")
            .and_then(Value::as_u64)
            .and_then(|value| usize::try_from(value).ok())
            .ok_or_else(|| invalid_data("attempt recovery byte evidence is malformed"))?;
        let created_at_ms = u64::from_be_bytes(
            index_key
                .key_bytes()
                .get(..8)
                .ok_or_else(|| invalid_data("attempt recovery key is malformed"))?
                .try_into()
                .unwrap(),
        );
        if index_key != recovery_index_key(transaction, created_at_ms, &attempt_id)? {
            return Err(invalid_data("attempt recovery key identity differs"));
        }
        if recovered > 0
            && (recovered >= request.max_rows
                || projection_bytes.saturating_add(indexed_projection_bytes) > request.max_bytes)
        {
            remaining += 1;
            continue;
        }
        let loaded_attempt = load_attempt_for_update(database, transaction, &attempt_id)?
            .ok_or_else(|| invalid_data("attempt recovery target is missing"))?;
        if !attempt_is_live(&loaded_attempt.document)
            || recovery_index_value(&loaded_attempt.document, indexed_projection_bytes)?
                != encoded_locator
        {
            return Err(invalid_data("attempt recovery index target differs"));
        }
        let loaded_turn = load_current_turn_for_attempt(
            database,
            transaction,
            &attempt_id,
            &loaded_attempt.document,
            &loaded_attempt.conversation_id,
        )?
        .ok_or_else(|| invalid_data("attempt recovery Turn is missing"))?;
        let projection = loaded_turn
            .document
            .get("projection")
            .and_then(Value::as_object)
            .cloned()
            .ok_or_else(|| invalid_data("turn projection is malformed"))?;
        let row_bytes = serde_json::to_vec(&projection)
            .map_err(|_| invalid_data("turn projection cannot be encoded"))?
            .len();
        if row_bytes != indexed_projection_bytes {
            return Err(invalid_data(
                "attempt recovery projection byte evidence differs",
            ));
        }

        let settlement = restart_settlement(&loaded_attempt.document, &projection);
        let mut attempt = loaded_attempt.document.clone();
        attempt.insert("status".to_owned(), Value::String("interrupted".to_owned()));
        attempt.insert("settledAt".to_owned(), Value::from(request.now_ms));
        store_attempt_document(
            database,
            transaction,
            &attempt_id,
            &loaded_attempt,
            &attempt,
            request.now_ms,
        )?;

        let mut turn = loaded_turn.document.clone();
        let turn_id = text("turnId")?.to_owned();
        let conversation_id = text("conversationId")?.to_owned();
        let lane_id = turn
            .get("laneId")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid_data("turn lane identity is malformed"))?
            .to_owned();
        let ordinal = turn
            .get("ordinal")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
        let previous_updated_at_ms = turn
            .get("updatedAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("turn update timestamp is malformed"))?;
        let previous_revision = turn
            .get("projectionRevision")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
        let next_revision = previous_revision
            .checked_add(1)
            .ok_or_else(|| invalid_data("turn projection revision overflow"))?;
        retire_projection_head(
            database,
            transaction,
            &conversation_id,
            &turn_id,
            loaded_turn.projection_head.as_ref(),
        )?;
        turn.insert("projection".to_owned(), Value::Object(projection.clone()));
        turn.remove("_projectionHead");
        turn.insert("status".to_owned(), Value::String("interrupted".to_owned()));
        turn.insert("settlement".to_owned(), settlement.clone());
        turn.insert("projectionRevision".to_owned(), Value::from(next_revision));
        turn.insert("updatedAt".to_owned(), Value::from(request.now_ms));
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: loaded_turn.key.clone(),
                namespace: DOCUMENT_IDENTITY.to_owned(),
                logical_key: turn_logical_key(&conversation_id, &turn_id),
                value_json: serde_json::to_vec(&Value::Object(turn.clone()))
                    .map_err(|_| invalid_data("turn cannot be encoded"))?,
                expected_version: Some(loaded_turn.physical_version),
                updated_at_ms: request.now_ms,
            },
        )?;
        let staged = database
            .entity_get(transaction, &loaded_turn.key)?
            .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
        database.entity_put(
            transaction,
            lane_index_key(transaction, &conversation_id, &lane_id, ordinal)?,
            staged,
        )?;
        put_lane_compaction_index(database, transaction, &turn)?;
        database.entity_delete(
            transaction,
            updated_index_key(
                transaction,
                &conversation_id,
                previous_updated_at_ms,
                &turn_id,
            )?,
        )?;
        database.entity_put(
            transaction,
            updated_index_key(transaction, &conversation_id, request.now_ms, &turn_id)?,
            encode_updated_index_value(&turn_id, next_revision)?,
        )?;
        crate::search_dirty::mark(
            database,
            transaction,
            TURN_SEARCH_DIRTY_NAMESPACE,
            &conversation_id,
        )?;
        crate::conversation_header::advance_for_turn(
            database,
            transaction,
            &conversation_id,
            request.now_ms,
            request.now_ms,
            false,
        )?;
        append_attempt_event(
            database,
            transaction,
            AttemptEventAppend {
                conversation_id: &conversation_id,
                turn_id: &turn_id,
                attempt_id: &attempt_id,
                projection_revision: next_revision,
                event_type: "terminal_settlement",
                payload: json!({
                    "status": "interrupted",
                    "settlement": settlement,
                    "projectionPatch": projection_patch(
                        &projection,
                        &projection,
                        previous_revision,
                    )?,
                }),
                occurred_at_ms: request.now_ms,
                publish_conversation_sync: true,
            },
        )?;
        recovered += 1;
        projection_bytes = projection_bytes.saturating_add(row_bytes);
    }
    encode_response(&json!({"recovered": recovered, "remaining": remaining}))
}

pub(crate) fn events_list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    after: u64,
    limit: usize,
    patch_mode: bool,
) -> io::Result<Option<Vec<u8>>> {
    let Some(attempt) = load_attempt_for_update(database, transaction, attempt_id)? else {
        return Ok(None);
    };
    let expected_turn_id = attempt
        .document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?;
    let owner_turn_key = turn_key(transaction, &attempt.conversation_id, expected_turn_id)?;
    let Some(owner_turn_stored) = database.entity_get(transaction, &owner_turn_key)? else {
        return Ok(None);
    };
    let owner_turn = materialize_turn(database, transaction, &owner_turn_stored)?;
    if owner_turn.get("conversationId").and_then(Value::as_str)
        != Some(attempt.conversation_id.as_str())
        || owner_turn.get("turnId").and_then(Value::as_str) != Some(expected_turn_id)
    {
        return Err(invalid_data("attempt owner Turn identity differs"));
    }
    if after == u64::MAX {
        return encode_response(&Value::Array(Vec::new())).map(Some);
    }
    let prefix = attempt_event_prefix(attempt_id)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_EVENT_NAMESPACE,
        &prefix,
    )?;
    let mut start = attempt_event_key(transaction, attempt_id, after + 1)?;
    let mut events = Vec::with_capacity(limit.min(INDEX_PAGE_ROWS));
    let mut response_bytes = 2_usize;
    while events.len() < limit {
        let page_limit = (limit - events.len()).min(INDEX_PAGE_ROWS);
        let rows = database.entity_scan(transaction, &start, &end, page_limit)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().expect("nonempty attempt event page").0,
            transaction,
            ATTEMPT_EVENT_NAMESPACE,
        )?;
        for (key, stored) in rows {
            let key_bytes = key.key_bytes();
            if key_bytes.len() != prefix.len() + 8 || !key_bytes.starts_with(&prefix) {
                return Err(invalid_data("attempt event key identity is malformed"));
            }
            let sequence = u64::from_be_bytes(
                key_bytes[prefix.len()..]
                    .try_into()
                    .expect("attempt event sequence is eight bytes"),
            );
            let (_, document_json) = crate::versioned_document::materialize_stored_document(
                database,
                transaction.tenant_id(),
                transaction.owner_user_id(),
                &stored,
                ATTEMPT_EVENT_DOCUMENT_IDENTITY,
            )?;
            let event = serde_json::from_slice::<Value>(&document_json)
                .ok()
                .and_then(|value| value.as_object().cloned())
                .ok_or_else(|| invalid_data("attempt event document is malformed"))?;
            if event.get("attemptId").and_then(Value::as_str) != Some(attempt_id)
                || event.get("seq").and_then(Value::as_u64) != Some(sequence)
                || event.get("conversationId").and_then(Value::as_str)
                    != Some(attempt.conversation_id.as_str())
                || event.get("turnId").and_then(Value::as_str) != Some(expected_turn_id)
            {
                return Err(invalid_data("attempt event document identity differs"));
            }
            response_bytes = response_bytes
                .checked_add(document_json.len())
                .and_then(|bytes| bytes.checked_add(usize::from(!events.is_empty())))
                .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "attempt event response exceeds 8 MiB",
                    )
                })?;
            events.push(Value::Object(event));
        }
        if row_count < page_limit {
            break;
        }
        start = continuation;
    }

    if !patch_mode {
        let hydrate_index = events.iter().rposition(|event| {
            matches!(
                event.get("type").and_then(Value::as_str),
                Some("projection_updated" | "interaction_request" | "terminal_settlement")
            ) && event
                .get("payload")
                .and_then(Value::as_object)
                .is_some_and(|payload| !payload.contains_key("projection"))
        });
        if let Some(index) = hydrate_index {
            if owner_turn.get("currentAttemptId").and_then(Value::as_str) == Some(attempt_id) {
                let projection = owner_turn
                    .get("projection")
                    .cloned()
                    .ok_or_else(|| invalid_data("turn projection is missing"))?;
                events[index]
                    .get_mut("payload")
                    .and_then(Value::as_object_mut)
                    .expect("hydration candidate has an object payload")
                    .insert("projection".to_owned(), projection);
            }
        }
    }
    encode_response(&Value::Array(events)).map(Some)
}

pub(crate) fn events_prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &EventPruneRequest,
) -> io::Result<Vec<u8>> {
    if request.settled_before_ms == 0
        || !(1..=256).contains(&request.max_attempts)
        || !(1..=MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION).contains(&request.max_rows)
    {
        return Err(invalid_input("invalid attempt event prune bounds"));
    }
    let physical_attempt_limit = request
        .max_attempts
        .min(MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION);
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_EVENT_RETENTION_INDEX_NAMESPACE,
        &[],
    )?;
    let end = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_EVENT_RETENTION_INDEX_NAMESPACE,
        &request.settled_before_ms.to_be_bytes(),
    )?;
    let mut candidates = database.entity_scan(
        transaction,
        &start,
        &end,
        physical_attempt_limit.saturating_add(1),
    )?;
    let overflow = candidates.len() > physical_attempt_limit;
    candidates.truncate(physical_attempt_limit);

    let mut deleted_rows = 0_usize;
    let mut deleted_attempts = 0_usize;
    let mut completed_candidates = 0_usize;
    let mut materialized_bytes = 0_usize;
    for (index_key, encoded_locator) in &candidates {
        if deleted_rows >= request.max_rows {
            break;
        }
        let locator = serde_json::from_slice::<Value>(encoded_locator)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("attempt retention index is malformed"))?;
        let text = |field: &str| {
            locator
                .get(field)
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| invalid_data("attempt retention identity is malformed"))
        };
        let attempt_id = text("attemptId")?.to_owned();
        let conversation_id = text("conversationId")?.to_owned();
        let turn_id = text("turnId")?.to_owned();
        let settled_at_ms = locator
            .get("settledAt")
            .and_then(Value::as_u64)
            .filter(|value| *value < request.settled_before_ms)
            .ok_or_else(|| invalid_data("attempt retention timestamp is malformed"))?;
        let next_sequence = locator
            .get("nextSequence")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0)
            .ok_or_else(|| invalid_data("attempt retention cursor is malformed"))?;
        if *index_key != attempt_event_retention_index_key(transaction, settled_at_ms, &attempt_id)?
        {
            return Err(invalid_data("attempt retention key identity differs"));
        }
        let loaded = load_attempt_for_update(database, transaction, &attempt_id)?
            .ok_or_else(|| invalid_data("attempt retention target is missing"))?;
        let attempt_bytes = serde_json::to_vec(&loaded.document)
            .map_err(|_| invalid_data("attempt retention target cannot be encoded"))?
            .len();
        if materialized_bytes.saturating_add(attempt_bytes)
            > MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES
        {
            break;
        }
        materialized_bytes += attempt_bytes;
        if loaded.conversation_id != conversation_id
            || loaded.document.get("turnId").and_then(Value::as_str) != Some(&turn_id)
            || terminal_attempt_settled_at(&loaded.document)? != Some(settled_at_ms)
            || attempt_event_retention_index_value(&loaded.document, next_sequence)?
                != *encoded_locator
        {
            return Err(invalid_data("attempt retention index target differs"));
        }

        let reference_prefix = attempt_sync_reference_prefix(&attempt_id)?;
        let (reference_start, reference_end) = EntityKey::prefix_range(
            transaction.tenant_id(),
            transaction.owner_user_id(),
            ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE,
            &reference_prefix,
        )?;
        if !database
            .entity_scan(transaction, &reference_start, &reference_end, 1)?
            .is_empty()
        {
            continue;
        }

        let stored_turn = database
            .entity_get(
                transaction,
                &turn_key(transaction, &conversation_id, &turn_id)?,
            )?
            .ok_or_else(|| invalid_data("attempt retention Turn is missing"))?;
        let (_, turn_json) = crate::versioned_document::materialize_stored_document(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &stored_turn,
            DOCUMENT_IDENTITY,
        )?;
        if materialized_bytes.saturating_add(turn_json.len())
            > MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES
        {
            break;
        }
        materialized_bytes += turn_json.len();
        let physical_turn = decode_turn_value(&turn_json)?;
        if physical_turn.get("conversationId").and_then(Value::as_str) != Some(&conversation_id)
            || physical_turn.get("turnId").and_then(Value::as_str) != Some(&turn_id)
        {
            return Err(invalid_data("attempt retention Turn identity differs"));
        }
        if physical_turn
            .get("currentAttemptId")
            .and_then(Value::as_str)
            == Some(&attempt_id)
            && projection_head_from_document(&physical_turn)?.is_some()
        {
            continue;
        }

        let head_key = attempt_event_head_key(transaction, &attempt_id)?;
        let head = decode_u64(
            database.entity_get(transaction, &head_key)?,
            "attempt event head is malformed",
        )?;
        if head == 0 {
            database.entity_delete(transaction, index_key.clone())?;
            completed_candidates += 1;
            continue;
        }
        if next_sequence > head {
            return Err(invalid_data(
                "attempt retention cursor exceeds its event head",
            ));
        }
        let available = usize::try_from(head - next_sequence + 1).unwrap_or(usize::MAX);
        let row_budget = request.max_rows - deleted_rows;
        let retiring = available.min(row_budget);
        let range_start = attempt_event_key(transaction, &attempt_id, next_sequence)?;
        let completes_attempt = retiring == available;
        let range_end = if completes_attempt {
            EntityKey::prefix_range(
                transaction.tenant_id(),
                transaction.owner_user_id(),
                ATTEMPT_EVENT_NAMESPACE,
                &attempt_event_prefix(&attempt_id)?,
            )?
            .1
        } else {
            attempt_event_key(transaction, &attempt_id, next_sequence + retiring as u64)?
        };
        database.entity_retire_range(transaction, &range_start, &range_end)?;
        deleted_rows += retiring;
        if completes_attempt {
            database.entity_delete(transaction, head_key)?;
            database.entity_delete(transaction, index_key.clone())?;
            deleted_attempts += 1;
            completed_candidates += 1;
        } else {
            database.entity_put(
                transaction,
                index_key.clone(),
                attempt_event_retention_index_value(
                    &loaded.document,
                    next_sequence + retiring as u64,
                )?,
            )?;
            break;
        }
    }
    let remaining =
        usize::from(overflow).saturating_add(candidates.len().saturating_sub(completed_candidates));
    encode_response(&json!({
        "deleted_rows": deleted_rows,
        "deleted_attempts": deleted_attempts,
        "remaining": remaining,
    }))
}

pub(crate) fn sync_prune(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &SyncPruneRequest,
) -> io::Result<Vec<u8>> {
    if request.created_before_ms == 0 || !(1..=20_000).contains(&request.max_rows) {
        return Err(invalid_input("invalid conversation sync prune bounds"));
    }
    // Reserve one bounded scan row as the continuation witness.  Entity scans
    // accept at most 1,000 rows, so asking for cap + 1 would reject the whole
    // maintenance transaction before it could make progress.
    let physical_limit = request
        .max_rows
        .min(MAX_SYNC_PRUNE_ROWS_PER_TRANSACTION.saturating_sub(1));
    let start = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_SYNC_AGE_INDEX_NAMESPACE,
        &[],
    )?;
    let end = EntityKey::new(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_SYNC_AGE_INDEX_NAMESPACE,
        &request.created_before_ms.to_be_bytes(),
    )?;
    let mut candidates =
        database.entity_scan(transaction, &start, &end, physical_limit.saturating_add(1))?;
    let overflow = candidates.len() > physical_limit;
    candidates.truncate(physical_limit);
    let mut deleted_rows = 0_usize;
    let mut processed = 0_usize;
    let mut materialized_bytes = 0_usize;
    for (age_key, encoded_locator) in &candidates {
        let locator = serde_json::from_slice::<Value>(encoded_locator)
            .ok()
            .and_then(|value| value.as_object().cloned())
            .ok_or_else(|| invalid_data("conversation sync retention index is malformed"))?;
        let conversation_id = locator
            .get("conversationId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| invalid_data("conversation sync retention identity is malformed"))?
            .to_owned();
        let sync_sequence = locator
            .get("syncSeq")
            .and_then(Value::as_u64)
            .filter(|value| *value > 0)
            .ok_or_else(|| invalid_data("conversation sync retention sequence is malformed"))?;
        let occurred_at_ms = locator
            .get("occurredAt")
            .and_then(Value::as_u64)
            .filter(|value| *value < request.created_before_ms)
            .ok_or_else(|| invalid_data("conversation sync retention timestamp is malformed"))?;
        if *age_key
            != conversation_sync_age_index_key(
                transaction,
                occurred_at_ms,
                &conversation_id,
                sync_sequence,
            )?
        {
            return Err(invalid_data(
                "conversation sync retention key identity differs",
            ));
        }
        let event_key = conversation_sync_event_key(transaction, &conversation_id, sync_sequence)?;
        let Some(stored) = database.entity_get(transaction, &event_key)? else {
            // Conversation deletion retires its replay range, while this
            // reconstructible age marker remains owner-global until TTL.  An
            // attempt-event locator carries enough evidence to retire its
            // owner-global protection reference without reopening the event.
            if let Some(attempt_id) = locator.get("attemptId").and_then(Value::as_str) {
                let attempt_sequence = locator
                    .get("attemptSequence")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("attempt sync reference sequence is malformed"))?;
                let reference_key = attempt_sync_reference_key(
                    transaction,
                    attempt_id,
                    &conversation_id,
                    sync_sequence,
                )?;
                if let Some(reference) = database.entity_get(transaction, &reference_key)? {
                    if reference.as_slice() != attempt_sequence.to_le_bytes() {
                        return Err(invalid_data("attempt sync reference target differs"));
                    }
                    database.entity_delete(transaction, reference_key)?;
                }
            } else if locator.get("attemptSequence").is_some() {
                return Err(invalid_data("attempt sync reference identity is malformed"));
            }
            database.entity_delete(transaction, age_key.clone())?;
            processed += 1;
            continue;
        };
        let (_, event_json) = crate::versioned_document::materialize_stored_document(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &stored,
            SYNC_DOCUMENT_IDENTITY,
        )?;
        if materialized_bytes.saturating_add(event_json.len()) > MAX_SYNC_PRUNE_MATERIALIZED_BYTES {
            break;
        }
        materialized_bytes += event_json.len();
        let event = serde_json::from_slice::<Value>(&event_json)
            .map_err(|_| invalid_data("conversation sync event is malformed"))?;
        if event.get("conversationId").and_then(Value::as_str) != Some(&conversation_id)
            || event.get("syncSeq").and_then(Value::as_u64) != Some(sync_sequence)
            || event.get("occurredAt").and_then(Value::as_u64) != Some(occurred_at_ms)
            || sync_age_index_value(&event)? != *encoded_locator
        {
            return Err(invalid_data(
                "conversation sync retention index target differs",
            ));
        }
        if event.get("type").and_then(Value::as_str) == Some("attempt.event") {
            let attempt_id = event
                .get("attemptId")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| invalid_data("attempt sync reference identity is malformed"))?;
            let attempt_sequence = event
                .get("payload")
                .and_then(|payload| payload.get("event"))
                .and_then(|attempt_event| attempt_event.get("seq"))
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("attempt sync reference sequence is malformed"))?;
            let reference_key = attempt_sync_reference_key(
                transaction,
                attempt_id,
                &conversation_id,
                sync_sequence,
            )?;
            let reference = database
                .entity_get(transaction, &reference_key)?
                .ok_or_else(|| invalid_data("attempt sync reference is missing"))?;
            if reference.as_slice() != attempt_sequence.to_le_bytes() {
                return Err(invalid_data("attempt sync reference target differs"));
            }
            database.entity_delete(transaction, reference_key)?;
        }
        database.entity_delete(transaction, event_key)?;
        database.entity_delete(transaction, age_key.clone())?;
        deleted_rows += 1;
        processed += 1;
    }
    encode_response(&json!({
        "deletedRows": deleted_rows,
        "remaining": overflow || processed < candidates.len(),
    }))
}

fn attempts_for_turns(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    turns: &[Value],
) -> io::Result<Vec<Value>> {
    let mut attempts: Vec<Value> = Vec::new();
    for turn in turns {
        let Some(attempt_id) = turn.get("currentAttemptId").and_then(Value::as_str) else {
            continue;
        };
        let encoded = attempt_get(database, transaction, attempt_id)?
            .ok_or_else(|| invalid_data("turn current attempt is missing"))?;
        attempts.push(
            serde_json::from_slice(&encoded)
                .map_err(|_| invalid_data("attempt response is malformed"))?,
        );
    }
    attempts.sort_by(|left, right| {
        let left_key = (
            left.get("createdAt").and_then(Value::as_u64).unwrap_or(0),
            left.get("attemptId").and_then(Value::as_str).unwrap_or(""),
        );
        let right_key = (
            right.get("createdAt").and_then(Value::as_u64).unwrap_or(0),
            right.get("attemptId").and_then(Value::as_str).unwrap_or(""),
        );
        left_key.cmp(&right_key)
    });
    Ok(attempts)
}

fn lane_total(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<usize> {
    let key = lane_count_key(transaction, conversation_id, lane_id)?;
    usize::try_from(decode_u64(
        database.entity_get(transaction, &key)?,
        "turn lane count is malformed",
    )?)
    .map_err(|_| invalid_data("turn lane count exceeds platform bounds"))
}

fn conversation_is_linear(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<bool> {
    let conversation = conversation_prefix(conversation_id)?;
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_HEAD_NAMESPACE,
        &conversation,
    )?;
    let lanes = database.entity_scan(transaction, &start, &end, 2)?;
    if lanes.is_empty() {
        return Ok(true);
    }
    Ok(lanes.len() == 1 && lanes[0].0.key_bytes() == lane_prefix(conversation_id, "main")?)
}

fn lane_tail_before(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    before_ordinal: u64,
    limit: usize,
) -> io::Result<(Vec<Value>, bool)> {
    let prefix = lane_prefix(conversation_id, lane_id)?;
    let (start, namespace_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    let end = if before_ordinal == u64::MAX {
        namespace_end
    } else {
        lane_index_key(transaction, conversation_id, lane_id, before_ordinal)?
    };
    let rows = database.entity_scan_reverse(transaction, &start, &end, limit + 1)?;
    let has_more = rows.len() > limit;
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let mut turns = rows
        .into_iter()
        .take(limit)
        .map(|(_, stored)| {
            materialize_turn(database, transaction, &stored)
                .and_then(|turn| public_turn(turn, execution_epoch))
        })
        .collect::<io::Result<Vec<_>>>()?;
    turns.reverse();
    Ok((turns, has_more))
}

pub(crate) fn sync_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_limit: usize,
    include_artifact_hint: bool,
) -> io::Result<Option<Vec<u8>>> {
    let Some((revision, settings)) =
        crate::conversation_header::sync_header(database, transaction, conversation_id)?
    else {
        return Ok(None);
    };
    let turns;
    let mut turn_window = None;
    if turn_limit != 0 && conversation_is_linear(database, transaction, conversation_id)? {
        let total = lane_total(database, transaction, conversation_id, "main")?;
        let (window, has_more) = lane_tail_before(
            database,
            transaction,
            conversation_id,
            "main",
            u64::MAX,
            turn_limit,
        )?;
        turns = window;
        turn_window = Some(json!({
            "laneId": "main",
            "nextBeforeOrdinal": if has_more { turns.first().and_then(|turn| turn.get("ordinal")).cloned().unwrap_or(Value::Null) } else { Value::Null },
            "hasMore": has_more,
            "totalTurns": total
        }));
    } else {
        turns = list_values(database, transaction, conversation_id, None)?;
    }
    let attempts = attempts_for_turns(database, transaction, &turns)?;
    let mut response = json!({
        "conversationId": conversation_id,
        "conversationRevision": revision,
        "syncSequence": sync_head(database, transaction, conversation_id)?,
        "settings": settings,
        "turns": turns,
        "attempts": attempts,
        "queueItems": []
    });
    if let Some(turn_window) = turn_window {
        response["turnWindow"] = turn_window;
    }
    if include_artifact_hint {
        response["hasArtifacts"] = Value::Bool(false);
    }
    encode_response(&response).map(Some)
}

pub(crate) fn sync_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    before_ordinal: u64,
    limit: usize,
    expected_sync_sequence: u64,
) -> io::Result<Option<Vec<u8>>> {
    let Some((revision, _)) =
        crate::conversation_header::sync_header(database, transaction, conversation_id)?
    else {
        return Ok(None);
    };
    let head = sync_head(database, transaction, conversation_id)?;
    if head != expected_sync_sequence {
        return encode_response(&json!({"stale": true, "syncSequence": head})).map(Some);
    }
    let total = lane_total(database, transaction, conversation_id, lane_id)?;
    let (turns, has_more) = lane_tail_before(
        database,
        transaction,
        conversation_id,
        lane_id,
        before_ordinal,
        limit,
    )?;
    let next_before_ordinal = turns
        .first()
        .and_then(|turn| turn.get("ordinal"))
        .cloned()
        .unwrap_or(Value::Null);
    let attempts = attempts_for_turns(database, transaction, &turns)?;
    encode_response(&json!({
        "conversationId": conversation_id,
        "conversationRevision": revision,
        "syncSequence": head,
        "laneId": lane_id,
        "beforeOrdinal": before_ordinal,
        "nextBeforeOrdinal": next_before_ordinal,
        "hasMore": has_more,
        "totalTurns": total,
        "turns": turns,
        "attempts": attempts
    }))
    .map(Some)
}

pub(crate) fn sync_changes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    after: u64,
    limit: usize,
) -> io::Result<Option<Vec<u8>>> {
    if crate::conversation_header::sync_header(database, transaction, conversation_id)?.is_none() {
        return Ok(None);
    }
    let head = sync_head(database, transaction, conversation_id)?;
    if after > head {
        return encode_response(&json!({
            "head": head, "events": [], "resetRequired": true, "resetReason": "cursor_invalid"
        }))
        .map(Some);
    }
    let mut prefix = conversation_prefix(conversation_id)?;
    prefix.extend_from_slice(&after.saturating_add(1).to_be_bytes());
    let start = entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &prefix)?;
    let (_, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        CONVERSATION_SYNC_EVENT_NAMESPACE,
        &conversation_prefix(conversation_id)?,
    )?;
    let mut events = Vec::with_capacity(limit.min(INDEX_PAGE_ROWS));
    let mut response_bytes = 128_usize;
    let mut cursor = start;
    while events.len() < limit {
        let page_limit = (limit - events.len()).min(INDEX_PAGE_ROWS);
        let rows = database.entity_scan(transaction, &cursor, &end, page_limit)?;
        if rows.is_empty() {
            break;
        }
        let row_count = rows.len();
        let continuation = after_key(
            &rows.last().unwrap().0,
            transaction,
            CONVERSATION_SYNC_EVENT_NAMESPACE,
        )?;
        for (_, stored) in rows {
            let (_, value) = crate::versioned_document::materialize_stored_document(
                database,
                transaction.tenant_id(),
                transaction.owner_user_id(),
                &stored,
                SYNC_DOCUMENT_IDENTITY,
            )?;
            response_bytes = response_bytes
                .checked_add(value.len() + usize::from(!events.is_empty()))
                .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "conversation sync page exceeds 8 MiB",
                    )
                })?;
            events.push(
                serde_json::from_slice::<Value>(&value)
                    .map_err(|_| invalid_data("conversation sync event is malformed"))?,
            );
        }
        if row_count < page_limit {
            break;
        }
        cursor = continuation;
    }
    let contiguous = events.iter().enumerate().all(|(index, event)| {
        event.get("syncSeq").and_then(Value::as_u64) == Some(after + index as u64 + 1)
    });
    if head > after && (events.is_empty() || !contiguous) {
        return encode_response(&json!({
            "head": head, "events": [], "resetRequired": true, "resetReason": "cursor_expired"
        }))
        .map(Some);
    }
    let last = events
        .last()
        .and_then(|event| event.get("syncSeq"))
        .and_then(Value::as_u64)
        .unwrap_or(after);
    encode_response(&json!({
        "head": head,
        "events": events,
        "resetRequired": false,
        "hasMore": last < head
    }))
    .map(Some)
}

pub(crate) fn revision(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<Vec<u8>> {
    encode_response(&Value::from(crate::conversation_header::revision(
        database,
        transaction,
        conversation_id,
    )?))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn restart_settlement_preserves_prefill_checkpoint_and_human_guidance_options() {
        let attempt = json!({"_config": {"model": "gpt-4o"}})
            .as_object()
            .unwrap()
            .clone();
        let projection = json!({
            "content": "partial 🚀",
            "thinking": "working",
            "toolRounds": [
                {
                    "toolCallId": "call-1", "toolName": "search",
                    "toolArgs": "not-json", "toolContent": "result"
                },
                {
                    "_providerAttemptDiscarded": true,
                    "toolCallId": "discarded", "toolName": "search"
                },
                {
                    "toolCallId": "ask-1", "toolName": "ask_human",
                    "status": "awaiting_human", "guidanceId": "guide-1",
                    "guidanceQuestion": "Continue?", "guidanceType": "confirm"
                }
            ]
        })
        .as_object()
        .unwrap()
        .clone();
        let settlement = restart_settlement(&attempt, &projection);
        assert_eq!(settlement["cause"], "server_restart");
        assert_eq!(settlement["resumeOptions"][0]["operation"], "continue");
        assert_eq!(settlement["resumeOptions"][0]["anchor"]["contentChars"], 9);
        assert_eq!(
            settlement["resumeOptions"][1]["anchor"],
            json!({
                "type": "tool_checkpoint",
                "keptToolRounds": 2,
                "replayableToolRounds": 1,
                "retainedToolRoundPositions": [0],
                "content": "", "thinking": "", "segments": []
            })
        );
        assert_eq!(
            settlement["resumeOptions"][2]["operation"],
            "answer_guidance"
        );
        assert_eq!(
            settlement["resumeOptions"][2]["anchor"]["retainedToolRoundPositions"],
            json!([0])
        );
        assert_eq!(settlement["resumeOptions"][3]["operation"], "regenerate");

        let claude = json!({"_config": {"model": "aws.anthropic.claude-opus-5"}})
            .as_object()
            .unwrap()
            .clone();
        let prose_only = json!({"content": "partial"}).as_object().unwrap().clone();
        assert_eq!(
            restart_settlement(&claude, &prose_only)["resumeOptions"],
            json!([{"operation": "regenerate", "anchor": {"type": "turn_start"}}])
        );
    }

    fn live_projection_request(attempt_id: &str, content: &str, now_ms: u64) -> EventRecordRequest {
        EventRecordRequest {
            attempt_id: attempt_id.to_owned(),
            task_id: "projection-head-task".to_owned(),
            terminal: false,
            status: "running".to_owned(),
            slim: true,
            content: content.to_owned(),
            thinking: "working".to_owned(),
            projection: Value::Null,
            projection_patch: None,
            settlement: Map::new(),
            error: Map::new(),
            event_type: "projection_updated".to_owned(),
            event_payload: Map::new(),
            task_event: None,
            now_ms,
        }
    }

    #[test]
    fn large_live_projection_uses_bounded_head_and_terminal_materializes_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let projection = json!({
            "content": "",
            "thinking": "",
            "largeStableEvidence": "x".repeat(128 * 1024),
        });
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "projection-head-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "projection-head-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "projection-head-turn".to_owned(),
                attempt_id: Some("projection-head-attempt".to_owned()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Projection head".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();

        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "projection-head-attempt",
            "projection-head-task",
            "",
            1_100,
        )
        .unwrap();
        database.commit(bind).unwrap();

        let mut live = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut live,
            &live_projection_request("projection-head-attempt", "first", 1_200),
        )
        .unwrap();
        database.commit(live).unwrap();

        let head = {
            let mut inspect = database.begin(7, 11).unwrap();
            let physical_turn_key = turn_key(
                &inspect,
                "projection-head-conversation",
                "projection-head-turn",
            )
            .unwrap();
            let stored = database
                .entity_get(&mut inspect, &physical_turn_key)
                .unwrap()
                .unwrap();
            let (_, physical_json) = crate::versioned_document::materialize_stored_document(
                &database,
                7,
                11,
                &stored,
                DOCUMENT_IDENTITY,
            )
            .unwrap();
            assert!(physical_json.len() < 8 * 1024);
            let physical = decode_turn_value(&physical_json).unwrap();
            assert_eq!(physical["projection"], json!({}));
            let head = projection_head_from_document(&physical).unwrap().unwrap();
            assert_eq!(head.patch_count, 1);
            let logical: Value = serde_json::from_slice(
                &get(
                    &database,
                    &mut inspect,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap()
                .unwrap(),
            )
            .unwrap();
            assert_eq!(logical["projection"]["content"], "first");
            assert_eq!(
                logical["projection"]["largeStableEvidence"]
                    .as_str()
                    .unwrap()
                    .len(),
                128 * 1024
            );
            let mut settled_with_head = physical.clone();
            settled_with_head.insert("status".to_owned(), Value::String("completed".to_owned()));
            assert_eq!(
                projection_head_from_document(&settled_with_head)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::InvalidData
            );
            let mut wrong_revision = physical;
            wrong_revision.insert("projectionRevision".to_owned(), Value::from(999));
            assert_eq!(
                projection_head_from_document(&wrong_revision)
                    .unwrap_err()
                    .kind(),
                io::ErrorKind::InvalidData
            );
            head
        };

        {
            let mut missing_checkpoint = database.begin(7, 11).unwrap();
            let key = projection_checkpoint_key(
                &missing_checkpoint,
                "projection-head-conversation",
                "projection-head-turn",
                &head.head_id,
            )
            .unwrap();
            database
                .entity_delete(&mut missing_checkpoint, key)
                .unwrap();
            assert_eq!(
                get(
                    &database,
                    &mut missing_checkpoint,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap_err()
                .kind(),
                io::ErrorKind::InvalidData
            );
        }
        {
            let mut missing_patch = database.begin(7, 11).unwrap();
            let key = projection_patch_key(
                &missing_patch,
                "projection-head-conversation",
                "projection-head-turn",
                &head.head_id,
                head.base_revision + 1,
            )
            .unwrap();
            database.entity_delete(&mut missing_patch, key).unwrap();
            assert_eq!(
                get(
                    &database,
                    &mut missing_patch,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap_err()
                .kind(),
                io::ErrorKind::InvalidData
            );
        }

        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut second = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut second,
            &live_projection_request("projection-head-attempt", "second", 1_300),
        )
        .unwrap();
        database.commit(second).unwrap();

        for index in 2..MAX_TURN_PROJECTION_HEAD_PATCHES {
            let mut frame = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            event_record(
                &database,
                &mut frame,
                &live_projection_request(
                    "projection-head-attempt",
                    &format!("frame-{index}"),
                    1_300 + index as u64,
                ),
            )
            .unwrap();
            database.commit(frame).unwrap();
        }
        {
            let mut inspect = database.begin(7, 11).unwrap();
            let key = turn_key(
                &inspect,
                "projection-head-conversation",
                "projection-head-turn",
            )
            .unwrap();
            let stored = database.entity_get(&mut inspect, &key).unwrap().unwrap();
            let (_, physical_json) = crate::versioned_document::materialize_stored_document(
                &database,
                7,
                11,
                &stored,
                DOCUMENT_IDENTITY,
            )
            .unwrap();
            let current =
                projection_head_from_document(&decode_turn_value(&physical_json).unwrap())
                    .unwrap()
                    .unwrap();
            assert_eq!(current.head_id, head.head_id);
            assert_eq!(current.patch_count, MAX_TURN_PROJECTION_HEAD_PATCHES);
        }
        let mut rollover = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut rollover,
            &live_projection_request("projection-head-attempt", "rollover", 2_000),
        )
        .unwrap();
        database.commit(rollover).unwrap();
        {
            let mut inspect = database.begin(7, 11).unwrap();
            let key = turn_key(
                &inspect,
                "projection-head-conversation",
                "projection-head-turn",
            )
            .unwrap();
            let stored = database.entity_get(&mut inspect, &key).unwrap().unwrap();
            let (_, physical_json) = crate::versioned_document::materialize_stored_document(
                &database,
                7,
                11,
                &stored,
                DOCUMENT_IDENTITY,
            )
            .unwrap();
            let current =
                projection_head_from_document(&decode_turn_value(&physical_json).unwrap())
                    .unwrap()
                    .unwrap();
            assert_ne!(current.head_id, head.head_id);
            assert_eq!(current.patch_count, 0);
            let logical: Value = serde_json::from_slice(
                &get(
                    &database,
                    &mut inspect,
                    "projection-head-conversation",
                    "projection-head-turn",
                )
                .unwrap()
                .unwrap(),
            )
            .unwrap();
            assert_eq!(logical["projection"]["content"], "rollover");
        }
        let mut terminal_request =
            live_projection_request("projection-head-attempt", "final", 2_100);
        terminal_request.terminal = true;
        terminal_request.status = "completed".to_owned();
        terminal_request.thinking = "done".to_owned();
        terminal_request.settlement = json!({
            "outcome": "completed",
            "cause": "finished",
            "resumeOptions": [],
        })
        .as_object()
        .unwrap()
        .clone();
        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(&database, &mut terminal, &terminal_request).unwrap();
        database.commit(terminal).unwrap();

        let mut verify = database.begin(7, 11).unwrap();
        let physical_turn_key = turn_key(
            &verify,
            "projection-head-conversation",
            "projection-head-turn",
        )
        .unwrap();
        let stored = database
            .entity_get(&mut verify, &physical_turn_key)
            .unwrap()
            .unwrap();
        let (_, physical_json) = crate::versioned_document::materialize_stored_document(
            &database,
            7,
            11,
            &stored,
            DOCUMENT_IDENTITY,
        )
        .unwrap();
        let physical = decode_turn_value(&physical_json).unwrap();
        assert!(projection_head_from_document(&physical).unwrap().is_none());
        assert_eq!(physical["projection"]["content"], "final");
        let (head_start, head_end) = projection_head_range(
            &verify,
            "projection-head-conversation",
            "projection-head-turn",
            &head.head_id,
        )
        .unwrap();
        assert!(database
            .entity_scan(&mut verify, &head_start, &head_end, 1)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn conversation_lifecycle_restores_and_purges_projection_heads() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let projection = json!({
            "content": "",
            "thinking": "",
            "largeStableEvidence": "x".repeat(128 * 1024),
        });
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: serde_json::to_vec(&projection).unwrap(),
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "projection-head-lifecycle-seed".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "projection-head-lifecycle-turn".to_owned(),
                attempt_id: Some("projection-head-lifecycle-attempt".to_owned()),
                created_at_ms: 1_000,
                committed_at_ms: 1_000,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Projection head lifecycle".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1_000,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "projection-head-lifecycle-attempt",
            "projection-head-task",
            "",
            1_100,
        )
        .unwrap();
        database.commit(bind).unwrap();
        let mut live = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut live,
            &live_projection_request("projection-head-lifecycle-attempt", "live", 1_200),
        )
        .unwrap();
        database.commit(live).unwrap();

        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &database,
            &mut delete,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                deleted_at_ms: 1_300,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();
        let mut restore = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::restore(
            &database,
            &mut restore,
            &crate::conversation_header::RestoreRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                committed_at_ms: 1_400,
            },
        )
        .unwrap();
        database.commit(restore).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let restored: Value = serde_json::from_slice(
            &get(
                &database,
                &mut read,
                "projection-head-lifecycle",
                "projection-head-lifecycle-turn",
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(restored["status"], "interrupted");
        assert_eq!(restored["projection"]["content"], "live");
        assert_eq!(
            restored["projection"]["largeStableEvidence"]
                .as_str()
                .unwrap()
                .len(),
            128 * 1024
        );
        drop(read);

        let mut purge = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::purge(
            &database,
            &mut purge,
            &crate::conversation_header::PurgeRequest {
                conversation_id: "projection-head-lifecycle".to_owned(),
                purged_at_ms: 1_500,
            },
        )
        .unwrap();
        database.commit(purge).unwrap();
        let mut verify = database.begin(7, 11).unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            TURN_PROJECTION_HEAD_NAMESPACE,
            &conversation_prefix("projection-head-lifecycle").unwrap(),
        )
        .unwrap();
        assert!(database
            .entity_scan(&mut verify, &start, &end, 1)
            .unwrap()
            .is_empty());
    }

    fn perception_observation(index: usize) -> Map<String, Value> {
        json!({
            "observationId": format!("paint:{index}"),
            "attemptId": "attempt-a",
            "kind": "phase_painted",
            "clientId": "page-a",
            "phase": "waiting_model",
            "detailKey": "status.waitingModel",
            "serverEmittedAt": 1_000 + index,
            "receivedAt": 1_125 + index,
            "paintedAt": 1_160 + index,
        })
        .as_object()
        .unwrap()
        .clone()
    }

    #[test]
    fn perception_observations_are_closed_idempotent_and_row_bounded() {
        let mut trace = Map::new();
        for index in 0..69 {
            let (next, applied) = append_perception_observation(
                &trace,
                &perception_observation(index),
                "task-a",
                "attempt-a",
                2_000 + index as u64,
            )
            .unwrap();
            assert!(applied);
            trace = next;
        }
        let observations = trace["clientObservations"].as_array().unwrap();
        assert_eq!(observations.len(), MAX_TIMING_TRACE_CLIENT_OBSERVATIONS);
        assert_eq!(trace["clientObservationDroppedCount"], 5);
        assert_eq!(observations.last().unwrap()["renderMs"], 35);
        assert_eq!(observations.last().unwrap()["transportMs"], 125);
        let (replayed, applied) = append_perception_observation(
            &trace,
            &perception_observation(68),
            "task-a",
            "attempt-a",
            9_999,
        )
        .unwrap();
        assert!(!applied);
        assert_eq!(replayed, trace);

        let mut forbidden = perception_observation(70);
        forbidden.insert("content".to_owned(), Value::String("secret".to_owned()));
        assert!(!perception_observation_is_valid(&forbidden, "attempt-a"));
    }

    #[test]
    fn timing_trace_compaction_enforces_the_durable_byte_ceiling() {
        let noisy_rows = (0..TRACE_MAX_SPANS)
            .map(|index| {
                json!({
                    "spanId": format!("span-{index}"),
                    "name": "x".repeat(400),
                    "attrs": (0..32).map(|child| {
                        (format!("field-{child}"), Value::String("y".repeat(400)))
                    }).collect::<Map<_, _>>()
                })
            })
            .collect::<Vec<_>>();
        let document = json!({
            "version": 1,
            "taskId": "task-budget",
            "summary": {"totalMs": 1, "overBudget": noisy_rows.clone()},
            "spans": noisy_rows,
            "statusHistory": (0..TRACE_MAX_STATUS_ENTRIES).map(|index| {
                json!({"id": format!("status-{index}"), "detail": "z".repeat(400)})
            }).collect::<Vec<_>>(),
            "clientObservations": (0..MAX_TIMING_TRACE_CLIENT_OBSERVATIONS).map(|index| {
                json!({"observationId": format!("paint-{index}"), "reason": "r".repeat(160)})
            }).collect::<Vec<_>>(),
        });
        let compacted = compact_timing_trace(document.as_object().unwrap()).unwrap();
        assert!(serde_json::to_vec(&compacted).unwrap().len() <= MAX_TIMING_TRACE_PERSISTED_BYTES);
        assert_eq!(compacted["compacted"], true);
        assert!(compacted["droppedSpans"].as_u64().unwrap_or(0) > 0);
    }

    fn append_live_attempt(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        turn_id: &str,
        attempt_id: &str,
    ) {
        append_settled(
            database,
            transaction,
            &AppendSettledRequest {
                conversation_id: conversation_id.to_owned(),
                actor: "assistant".to_owned(),
                status: "pending".to_owned(),
                projection_json: br#"{"content":"","thinking":"","segments":[],"toolRounds":[]}"#
                    .to_vec(),
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("command-{attempt_id}"),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: turn_id.to_owned(),
                attempt_id: Some(attempt_id.to_owned()),
                created_at_ms: 1,
                committed_at_ms: 1,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Claim fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1,
                },
            },
        )
        .unwrap();
    }

    fn append_historical_attempt(
        database: &AuthorityDatabase,
        transaction: &mut AuthorityTransaction,
        conversation_id: &str,
        turn_id: &str,
        current_attempt_id: &str,
        attempt_id: &str,
        created_at_ms: u64,
    ) {
        let attempt = Map::from_iter([
            ("attemptId".to_owned(), Value::String(attempt_id.to_owned())),
            (
                "conversationId".to_owned(),
                Value::String(conversation_id.to_owned()),
            ),
            ("turnId".to_owned(), Value::String(turn_id.to_owned())),
            (
                "commandId".to_owned(),
                Value::String(format!("command-{attempt_id}")),
            ),
            (
                "taskId".to_owned(),
                Value::String(format!("{attempt_id}-task")),
            ),
            (
                "operation".to_owned(),
                Value::String("regenerate".to_owned()),
            ),
            ("status".to_owned(), Value::String("completed".to_owned())),
            ("baseProjectionRevision".to_owned(), Value::from(1)),
            ("resumeAnchor".to_owned(), Value::Object(Map::new())),
            ("createdAt".to_owned(), Value::from(created_at_ms)),
            ("startedAt".to_owned(), Value::from(created_at_ms + 1)),
            ("settledAt".to_owned(), Value::from(created_at_ms + 2)),
            (
                "_dispatchMode".to_owned(),
                Value::String("conversation_executor".to_owned()),
            ),
        ]);
        let attempt_key = attempt_key(transaction, conversation_id, attempt_id).unwrap();
        crate::versioned_document::put(
            database,
            transaction,
            crate::versioned_document::PutRequest {
                key: attempt_key,
                namespace: "generation_attempts".to_owned(),
                logical_key: attempt_id.to_owned(),
                value_json: serde_json::to_vec(&Value::Object(attempt.clone())).unwrap(),
                expected_version: Some(0),
                updated_at_ms: created_at_ms + 2,
            },
        )
        .unwrap();
        let claim_key =
            global_identity_claim_key(transaction, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id).unwrap();
        database
            .entity_put(
                transaction,
                claim_key,
                encode_attempt_locator(transaction.owner_user_id(), conversation_id).unwrap(),
            )
            .unwrap();
        let previous_count = decode_u64(
            database
                .entity_get(
                    transaction,
                    &attempt_turn_count_key(transaction, conversation_id, turn_id).unwrap(),
                )
                .unwrap(),
            "turn attempt count is malformed",
        )
        .unwrap();
        append_attempt_turn_directory(
            database,
            transaction,
            conversation_id,
            turn_id,
            Some(current_attempt_id),
            &attempt,
        )
        .unwrap();
        let count_key = attempt_turn_count_key(transaction, conversation_id, turn_id).unwrap();
        database
            .entity_put(
                transaction,
                count_key,
                (previous_count + 1).to_le_bytes().to_vec(),
            )
            .unwrap();
        put_attempt_timing_indexes(database, transaction, &attempt).unwrap();
    }

    #[test]
    fn attempt_event_prune_is_owner_scoped_bounded_and_preserves_the_turn() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "retention-conversation",
            "retention-turn",
            "retention-attempt",
        );
        database.commit(seed).unwrap();

        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "retention-attempt",
            "projection-head-task",
            "",
            2,
        )
        .unwrap();
        database.commit(bind).unwrap();
        let mut live = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(
            &database,
            &mut live,
            &live_projection_request("retention-attempt", "partial", 3),
        )
        .unwrap();
        database.commit(live).unwrap();

        let request = EventPruneRequest {
            settled_before_ms: 10,
            max_attempts: 16,
            max_rows: 10,
        };
        let mut live_prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_prune(&database, &mut live_prune, &request).unwrap()
            )
            .unwrap(),
            json!({"deleted_rows":0,"deleted_attempts":0,"remaining":0})
        );
        drop(live_prune);

        let mut terminal_request = live_projection_request("retention-attempt", "final", 4);
        terminal_request.terminal = true;
        terminal_request.status = "completed".to_owned();
        terminal_request.settlement = json!({
            "outcome": "completed", "cause": "provider_finished", "resumeOptions": []
        })
        .as_object()
        .unwrap()
        .clone();
        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(&database, &mut terminal, &terminal_request).unwrap();
        database.commit(terminal).unwrap();

        let mut foreign = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_prune(&database, &mut foreign, &request).unwrap()
            )
            .unwrap(),
            json!({"deleted_rows":0,"deleted_attempts":0,"remaining":0})
        );
        drop(foreign);

        let mut first_sync_prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &sync_prune(
                    &database,
                    &mut first_sync_prune,
                    &SyncPruneRequest {
                        created_before_ms: 10,
                        max_rows: 1,
                    },
                )
                .unwrap()
            )
            .unwrap(),
            json!({"deletedRows":1,"remaining":true})
        );
        database.commit(first_sync_prune).unwrap();
        let mut protected = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_prune(&database, &mut protected, &request).unwrap()
            )
            .unwrap(),
            json!({"deleted_rows":0,"deleted_attempts":0,"remaining":1})
        );
        drop(protected);
        let mut finish_sync_prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &sync_prune(
                    &database,
                    &mut finish_sync_prune,
                    &SyncPruneRequest {
                        created_before_ms: 10,
                        max_rows: 10,
                    },
                )
                .unwrap()
            )
            .unwrap(),
            json!({"deletedRows":3,"remaining":false})
        );
        database.commit(finish_sync_prune).unwrap();

        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let first_result: Value = serde_json::from_slice(
            &events_prune(
                &database,
                &mut first,
                &EventPruneRequest {
                    max_rows: 1,
                    ..request.clone()
                },
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(
            first_result,
            json!({"deleted_rows":1,"deleted_attempts":0,"remaining":1})
        );
        database.commit(first).unwrap();

        let mut remaining = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let retained_events: Value = serde_json::from_slice(
            &events_list(&database, &mut remaining, "retention-attempt", 0, 10, true)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(retained_events.as_array().unwrap().len(), 2);
        drop(remaining);

        let mut finish = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let finish_result: Value =
            serde_json::from_slice(&events_prune(&database, &mut finish, &request).unwrap())
                .unwrap();
        assert_eq!(
            finish_result,
            json!({"deleted_rows":2,"deleted_attempts":1,"remaining":0})
        );
        database.commit(finish).unwrap();

        let mut verify = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &events_list(&database, &mut verify, "retention-attempt", 0, 10, true,)
                    .unwrap()
                    .unwrap()
            )
            .unwrap(),
            json!([])
        );
        let turn: Value = serde_json::from_slice(
            &get(
                &database,
                &mut verify,
                "retention-conversation",
                "retention-turn",
            )
            .unwrap()
            .unwrap(),
        )
        .unwrap();
        assert_eq!(turn["projection"]["content"], "final");
    }

    #[test]
    fn sync_prune_retires_attempt_references_after_conversation_delete() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "deleted-retention-conversation",
            "deleted-retention-turn",
            "deleted-retention-attempt",
        );
        database.commit(seed).unwrap();
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        attempt_bind(
            &database,
            &mut bind,
            "deleted-retention-attempt",
            "deleted-retention-task",
            "",
            2,
        )
        .unwrap();
        database.commit(bind).unwrap();
        let mut terminal_request = live_projection_request("deleted-retention-attempt", "final", 3);
        terminal_request.terminal = true;
        terminal_request.status = "completed".to_owned();
        terminal_request.settlement = json!({
            "outcome": "completed", "cause": "provider_finished", "resumeOptions": []
        })
        .as_object()
        .unwrap()
        .clone();
        let mut terminal = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        event_record(&database, &mut terminal, &terminal_request).unwrap();
        database.commit(terminal).unwrap();

        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &database,
            &mut delete,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "deleted-retention-conversation".to_owned(),
                deleted_at_ms: 4,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();

        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &sync_prune(
                    &database,
                    &mut prune,
                    &SyncPruneRequest {
                        created_before_ms: 10,
                        max_rows: 10,
                    },
                )
                .unwrap()
            )
            .unwrap(),
            json!({"deletedRows":0,"remaining":false})
        );
        database.commit(prune).unwrap();

        let mut verify = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let prefix = attempt_sync_reference_prefix("deleted-retention-attempt").unwrap();
        let (start, end) =
            EntityKey::prefix_range(7, 11, ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE, &prefix)
                .unwrap();
        assert!(database
            .entity_scan(&mut verify, &start, &end, 1)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn attempt_claim_is_owner_fenced_replayable_and_occ_serialized() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut seed,
            "claim-conversation",
            "claim-turn",
            "claim-attempt",
        );
        database.commit(seed).unwrap();

        let mut foreign = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut foreign, "claim-attempt", "worker-a", 2).unwrap(),
            b"false"
        );

        let mut first = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let mut racing = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut first, "claim-attempt", "worker-a", 2).unwrap(),
            b"true"
        );
        assert_eq!(
            attempt_claim(&database, &mut racing, "claim-attempt", "worker-b", 2).unwrap(),
            b"true"
        );
        database.commit(first).unwrap();
        assert!(database.commit(racing).is_err());

        let mut replay = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut replay, "claim-attempt", "worker-a", 3).unwrap(),
            b"true"
        );
        assert_eq!(
            attempt_claim(&database, &mut replay, "claim-attempt", "worker-b", 3).unwrap(),
            b"false"
        );
        drop(replay);
        let mut bind = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let bound: Value = serde_json::from_slice(
            &attempt_bind(
                &database,
                &mut bind,
                "claim-attempt",
                "task-a",
                "worker-a",
                6,
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(bound["taskId"], "task-a");
        assert_eq!(bound["status"], "pending");
        database.commit(bind).unwrap();

        let mut start = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let running: Value = serde_json::from_slice(
            &attempt_start(&database, &mut start, "claim-attempt", "task-a", 7).unwrap(),
        )
        .unwrap();
        assert_eq!(running["status"], "running");
        database.commit(start).unwrap();

        let mut projection_event = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_attempt_event(
            &database,
            &mut projection_event,
            AttemptEventAppend {
                conversation_id: "claim-conversation",
                turn_id: "claim-turn",
                attempt_id: "claim-attempt",
                projection_revision: 3,
                event_type: "projection_updated",
                payload: json!({
                    "projectionPatch": {
                        "version": 1,
                        "baseRevision": 2,
                        "targetRevision": 3,
                        "operations": []
                    }
                }),
                occurred_at_ms: 8,
                publish_conversation_sync: false,
            },
        )
        .unwrap();
        database.commit(projection_event).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let turn: Value = serde_json::from_slice(
            &get(&database, &mut read, "claim-conversation", "claim-turn")
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(turn["status"], "running");
        assert_eq!(turn["projectionRevision"], 3);
        assert_eq!(
            sync_head(&database, &mut read, "claim-conversation").unwrap(),
            3
        );
        let event_head_key = attempt_event_head_key(&read, "claim-attempt").unwrap();
        assert_eq!(
            decode_u64(
                database.entity_get(&mut read, &event_head_key).unwrap(),
                "attempt event head is malformed",
            )
            .unwrap(),
            3
        );
        let events: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "claim-attempt", 0, 5_000, true)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(events.as_array().unwrap().len(), 3);
        assert_eq!(events[0]["payload"]["dispatchState"], "queued");
        assert_eq!(events[1]["payload"]["dispatchState"], "running");
        assert_eq!(events[2]["type"], "projection_updated");
        let page: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "claim-attempt", 0, 1, false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(page.as_array().unwrap().len(), 1);
        assert_eq!(page[0]["seq"], 1);
        let hydrated: Value = serde_json::from_slice(
            &events_list(&database, &mut read, "claim-attempt", 2, 1, false)
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(hydrated[0]["payload"]["projection"]["content"], "");
        drop(read);

        let mut foreign_read = database.begin_with_identity_claim_scopes(7, 12).unwrap();
        assert_eq!(
            events_list(
                &database,
                &mut foreign_read,
                "claim-attempt",
                0,
                1_000,
                true,
            )
            .unwrap(),
            None
        );
        drop(foreign_read);

        let mut legacy_seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_live_attempt(
            &database,
            &mut legacy_seed,
            "legacy-claim-conversation",
            "legacy-claim-turn",
            "legacy-claim-attempt",
        );
        database.commit(legacy_seed).unwrap();
        let mut legacy = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut legacy, "legacy-claim-attempt", "", 4).unwrap(),
            b"true"
        );
        database.commit(legacy).unwrap();
        let mut legacy_replay = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_claim(&database, &mut legacy_replay, "legacy-claim-attempt", "", 5).unwrap(),
            b"false"
        );
    }

    #[test]
    fn activity_index_avoids_projection_results_and_legacy_fallback_is_all_or_nothing() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut write,
            &crate::conversation_header::CreateRequest {
                conversation_id: "activity-conversation".to_owned(),
                title: "Activity".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        for (ordinal, timestamp) in [100_u64, 200].into_iter().enumerate() {
            append_settled(
                &database,
                &mut write,
                &AppendSettledRequest {
                    conversation_id: "activity-conversation".to_owned(),
                    actor: "human".to_owned(),
                    status: "completed".to_owned(),
                    projection_json: serde_json::to_vec(&json!({
                        "content": "x".repeat(20_000),
                        "timestamp": timestamp,
                    }))
                    .unwrap(),
                    settlement_json: b"{}".to_vec(),
                    lane_id: "main".to_owned(),
                    command_id: format!("activity-command-{ordinal}"),
                    kind: "fixture".to_owned(),
                    run_id: String::new(),
                    turn_id: format!("activity-turn-{ordinal}"),
                    attempt_id: None,
                    created_at_ms: timestamp,
                    committed_at_ms: timestamp,
                    defaults: TurnDefaults {
                        allow_create: false,
                        title: String::new(),
                        settings_json: b"{}".to_vec(),
                        created_at_ms: timestamp,
                    },
                },
            )
            .unwrap();
        }
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        let mut remaining = crate::generated_tofudb_ir::MAX_ACTIVITY_TURN_ROWS_PER_QUERY;
        assert_eq!(
            activity_intervals(
                &database,
                &mut read,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap(),
            BTreeSet::from([0, 1])
        );
        assert_eq!(
            remaining,
            crate::generated_tofudb_ir::MAX_ACTIVITY_TURN_ROWS_PER_QUERY - 2
        );
        drop(read);
        let mut bounded = database.begin(7, 11).unwrap();
        let mut remaining = 1;
        assert_eq!(
            activity_intervals(
                &database,
                &mut bounded,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::OutOfMemory
        );
        drop(bounded);

        let mut remove = database.begin(7, 11).unwrap();
        for ordinal in 0..2 {
            let key = activity_index_key(&remove, "activity-conversation", ordinal).unwrap();
            database.entity_delete(&mut remove, key).unwrap();
        }
        database.commit(remove).unwrap();
        let mut legacy = database.begin(7, 11).unwrap();
        let mut remaining = 2;
        assert_eq!(
            activity_intervals(
                &database,
                &mut legacy,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap(),
            BTreeSet::from([0, 1])
        );
        drop(legacy);

        let mut partial = database.begin(7, 11).unwrap();
        let partial_key = activity_index_key(&partial, "activity-conversation", 0).unwrap();
        database
            .entity_put(&mut partial, partial_key, encode_activity_index_value(100))
            .unwrap();
        database.commit(partial).unwrap();
        let mut corrupt = database.begin(7, 11).unwrap();
        let mut remaining = 10;
        assert_eq!(
            activity_intervals(
                &database,
                &mut corrupt,
                "activity-conversation",
                1,
                &[0, 150, 250],
                &mut remaining,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn attempt_locator_is_versioned_bounded_and_legacy_read_compatible() {
        let encoded = encode_attempt_locator(11, "conversation").unwrap();
        match decode_attempt_locator(&encoded).unwrap() {
            AttemptLocator::Conversation {
                owner_user_id,
                conversation_id,
            } => {
                assert_eq!(owner_user_id, 11);
                assert_eq!(conversation_id, "conversation");
            }
            AttemptLocator::LegacyOwner(_) => panic!("new locator used legacy encoding"),
        }
        assert!(decode_attempt_locator(&encoded[..encoded.len() - 1]).is_err());
        let legacy = 11_u64.to_be_bytes();
        assert!(matches!(
            decode_attempt_locator(&legacy).unwrap(),
            AttemptLocator::LegacyOwner(11)
        ));
        assert!(decode_attempt_locator(&0_u64.to_be_bytes()).is_err());

        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let transaction = database.begin(7, 11).unwrap();
        let clustered = attempt_key(&transaction, "conversation", "attempt").unwrap();
        let (start, end) = EntityKey::prefix_range(
            7,
            11,
            GENERATION_ATTEMPT_NAMESPACE,
            &conversation_prefix("conversation").unwrap(),
        )
        .unwrap();
        assert!(start <= clustered && clustered < end);
        let legacy = legacy_attempt_key(&transaction, "attempt").unwrap();
        assert!(legacy < start || legacy >= end);
        drop(transaction);

        let legacy_attempt = json!({
            "attemptId": "legacy-attempt",
            "conversationId": "legacy-conversation",
            "turnId": "legacy-turn",
            "status": "completed"
        });
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::create(
            &database,
            &mut write,
            &crate::conversation_header::CreateRequest {
                conversation_id: "legacy-conversation".to_owned(),
                title: "Legacy".to_owned(),
                settings_json: b"{}".to_vec(),
                created_at_ms: 1,
                updated_at_ms: 1,
                committed_at_ms: 1,
            },
        )
        .unwrap();
        let legacy_document_key = legacy_attempt_key(&write, "legacy-attempt").unwrap();
        crate::versioned_document::put(
            &database,
            &mut write,
            crate::versioned_document::PutRequest {
                key: legacy_document_key,
                namespace: "generation_attempts".to_owned(),
                logical_key: "legacy-attempt".to_owned(),
                value_json: serde_json::to_vec(&legacy_attempt).unwrap(),
                expected_version: Some(0),
                updated_at_ms: 1,
            },
        )
        .unwrap();
        let claim = global_identity_claim_key(&write, ATTEMPT_ID_CLAIM_NAMESPACE, "legacy-attempt")
            .unwrap();
        database
            .entity_put(&mut write, claim, 11_u64.to_be_bytes().to_vec())
            .unwrap();
        database.commit(write).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(
                &attempt_get(&database, &mut read, "legacy-attempt")
                    .unwrap()
                    .unwrap()
            )
            .unwrap(),
            legacy_attempt
        );
        drop(read);
        let mut delete = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &database,
            &mut delete,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "legacy-conversation".to_owned(),
                deleted_at_ms: 2,
            },
        )
        .unwrap();
        database.commit(delete).unwrap();
        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            attempt_get(&database, &mut read, "legacy-attempt").unwrap(),
            None
        );
    }

    #[test]
    fn stale_execution_epoch_projects_a_fully_inert_turn() {
        let projected = public_turn(
            json!({
                "turnId": "turn",
                "conversationId": "conversation",
                "status": "running",
                "runId": "live-run",
                "currentAttemptId": "live-attempt",
                "_executionEpoch": 3,
                "projection": {
                    "isStreaming": true,
                    "nested": {"attemptId": "nested-attempt", "content": "keep"},
                    "toolRounds": [{"status": "running"}, {"status": "completed"}],
                    "segments": [{
                        "type": "tool_use",
                        "result": {"status": "running", "content": "keep"}
                    }],
                    "timingTrace": {"status": "running", "running": true}
                },
                "settlement": {}
            })
            .as_object()
            .unwrap()
            .clone(),
            4,
        )
        .unwrap();
        assert_eq!(projected["status"], "interrupted");
        assert_eq!(projected["runId"], "");
        assert_eq!(projected["currentAttemptId"], Value::Null);
        assert_eq!(projected["settlement"]["cause"], "conversation_deleted");
        assert!(projected.get("_executionEpoch").is_none());
        assert!(projected["projection"].get("isStreaming").is_none());
        assert!(projected["projection"]["nested"].get("attemptId").is_none());
        assert_eq!(projected["projection"]["nested"]["content"], "keep");
        assert_eq!(
            projected["projection"]["toolRounds"][0]["status"],
            "aborted"
        );
        assert_eq!(
            projected["projection"]["toolRounds"][1]["status"],
            "completed"
        );
        assert_eq!(
            projected["projection"]["segments"][0]["result"]["status"],
            "aborted"
        );
        assert_eq!(projected["projection"]["timingTrace"]["status"], "aborted");
        assert_eq!(projected["projection"]["timingTrace"]["running"], false);
    }

    #[test]
    fn delta_scan_refuses_an_incomplete_page_above_its_row_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        for index in 0..=MAX_DELTA_ROWS {
            let turn_id = format!("turn-{index:04}");
            let key = updated_index_key(&write, "conversation", index as u64, &turn_id).unwrap();
            database
                .entity_put(
                    &mut write,
                    key,
                    encode_updated_index_value(&turn_id, 1).unwrap(),
                )
                .unwrap();
        }
        database.commit(write).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            list_delta(&database, &mut read, "conversation", 0, &BTreeMap::new(), 1,)
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    #[test]
    fn delta_revision_index_codec_rejects_truncation_and_trailing_bytes() {
        let encoded = encode_updated_index_value("turn-a", 7).unwrap();
        assert_eq!(
            decode_updated_index_value(&encoded).unwrap(),
            ("turn-a".to_owned(), 7)
        );
        assert!(decode_updated_index_value(&encoded[..encoded.len() - 1]).is_err());
        let mut trailing = encoded;
        trailing.push(0);
        assert!(decode_updated_index_value(&trailing).is_err());
    }

    #[test]
    fn tombstone_pruning_is_bounded_and_releases_only_matching_identity_claims() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for index in 0..=MAX_TOMBSTONE_PRUNE_ROWS {
            let turn_id = format!("old-{index:03}");
            let attempt_id = format!("attempt-{index:03}");
            let tombstone_key = tombstone_index_key(&write, "conversation", 1, &turn_id).unwrap();
            let age_key = tombstone_age_index_key(&write, 1, "conversation", &turn_id).unwrap();
            let turn_claim_key =
                global_identity_claim_key(&write, TURN_ID_CLAIM_NAMESPACE, &turn_id).unwrap();
            let attempt_claim_key =
                global_identity_claim_key(&write, ATTEMPT_ID_CLAIM_NAMESPACE, &attempt_id).unwrap();
            database
                .entity_put(&mut write, tombstone_key, Vec::new())
                .unwrap();
            database
                .entity_put(&mut write, age_key, attempt_id.as_bytes().to_vec())
                .unwrap();
            database
                .entity_put(&mut write, turn_claim_key, 11_u64.to_be_bytes().to_vec())
                .unwrap();
            database
                .entity_put(&mut write, attempt_claim_key, 11_u64.to_be_bytes().to_vec())
                .unwrap();
        }
        let fresh_turn_id = "fresh";
        let fresh_at = TOMBSTONE_RETENTION_MS + 100;
        let fresh_tombstone_key =
            tombstone_index_key(&write, "conversation", fresh_at, fresh_turn_id).unwrap();
        let fresh_age_key =
            tombstone_age_index_key(&write, fresh_at, "conversation", fresh_turn_id).unwrap();
        let fresh_claim_key =
            global_identity_claim_key(&write, TURN_ID_CLAIM_NAMESPACE, fresh_turn_id).unwrap();
        database
            .entity_put(&mut write, fresh_tombstone_key, Vec::new())
            .unwrap();
        database
            .entity_put(&mut write, fresh_age_key, Vec::new())
            .unwrap();
        database
            .entity_put(&mut write, fresh_claim_key, 11_u64.to_be_bytes().to_vec())
            .unwrap();
        database.commit(write).unwrap();

        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            prune_expired_tombstones(&database, &mut prune, fresh_at).unwrap(),
            MAX_TOMBSTONE_PRUNE_ROWS
        );
        database.commit(prune).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        let (start, end) =
            EntityKey::prefix_range(7, 11, TURN_TOMBSTONE_AGE_INDEX_NAMESPACE, b"").unwrap();
        assert_eq!(
            database
                .entity_scan(&mut read, &start, &end, 10)
                .unwrap()
                .len(),
            2
        );
        let removed_claim_key =
            global_identity_claim_key(&read, TURN_ID_CLAIM_NAMESPACE, "old-000").unwrap();
        let retained_stale_claim_key =
            global_identity_claim_key(&read, TURN_ID_CLAIM_NAMESPACE, "old-256").unwrap();
        let retained_fresh_claim_key =
            global_identity_claim_key(&read, TURN_ID_CLAIM_NAMESPACE, fresh_turn_id).unwrap();
        assert!(database
            .entity_get(&mut read, &removed_claim_key)
            .unwrap()
            .is_none());
        assert!(database
            .entity_get(&mut read, &retained_stale_claim_key)
            .unwrap()
            .is_some());
        assert!(database
            .entity_get(&mut read, &retained_fresh_claim_key)
            .unwrap()
            .is_some());
    }

    #[test]
    fn delta_known_revision_skips_document_and_blob_materialization() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        let index_key = updated_index_key(&write, "conversation", 10, "unchanged").unwrap();
        database
            .entity_put(
                &mut write,
                index_key,
                encode_updated_index_value("unchanged", 9).unwrap(),
            )
            .unwrap();
        database.commit(write).unwrap();

        let mut known_revisions = BTreeMap::new();
        known_revisions.insert("unchanged".to_owned(), 9);
        let mut read = database.begin(7, 11).unwrap();
        let response = list_delta(
            &database,
            &mut read,
            "conversation",
            15,
            &known_revisions,
            20,
        )
        .unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&response).unwrap(),
            json!({"turns": [], "deletedTurnIds": [], "serverNowMs": 20})
        );
    }

    #[test]
    fn delta_response_budget_rejects_multiple_large_blob_turns() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for (ordinal, turn_id) in [(0_u64, "large-a"), (1, "large-b")] {
            let mut write = database.begin(7, 11).unwrap();
            let document = json!({
                "turnId": turn_id,
                "presentationId": turn_id,
                "conversationId": "conversation",
                "laneId": "main",
                "parentTurnId": Value::Null,
                "ordinal": ordinal,
                "actor": "assistant",
                "kind": "fixture",
                "runId": "",
                "status": "completed",
                "currentAttemptId": Value::Null,
                "projection": {"content": "x".repeat(4_200_000)},
                "projectionRevision": 1,
                "settlement": {},
                "createdAt": ordinal + 1,
                "updatedAt": ordinal + 1
            });
            let document_key = turn_key(&write, "conversation", turn_id).unwrap();
            crate::versioned_document::put(
                &database,
                &mut write,
                crate::versioned_document::PutRequest {
                    key: document_key,
                    namespace: DOCUMENT_IDENTITY.to_owned(),
                    logical_key: turn_logical_key("conversation", turn_id),
                    value_json: serde_json::to_vec(&document).unwrap(),
                    expected_version: Some(0),
                    updated_at_ms: ordinal + 1,
                },
            )
            .unwrap();
            let index_key =
                updated_index_key(&write, "conversation", ordinal + 1, turn_id).unwrap();
            database
                .entity_put(
                    &mut write,
                    index_key,
                    encode_updated_index_value(turn_id, 1).unwrap(),
                )
                .unwrap();
            database.commit(write).unwrap();
        }

        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            list_delta(
                &database,
                &mut read,
                "conversation",
                0,
                &BTreeMap::new(),
                10,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    #[test]
    fn attempt_directory_and_tombstone_bounds_are_explicit_and_legacy_compatible() {
        let attempt_ids = (0..MAX_ATTEMPTS_PER_TURN)
            .map(|index| format!("attempt-{index:03}-{}", "x".repeat(112)))
            .collect::<Vec<_>>();
        let encoded = encode_tombstone_attempt_ids(&attempt_ids).unwrap();
        assert!(encoded.len() <= MAX_ATTEMPT_TOMBSTONE_BYTES);
        assert_eq!(decode_tombstone_attempt_ids(&encoded).unwrap(), attempt_ids);
        assert_eq!(
            decode_tombstone_attempt_ids(b"legacy-attempt").unwrap(),
            vec!["legacy-attempt"]
        );
        let mut over_limit = attempt_ids;
        over_limit.push("one-too-many".to_owned());
        assert_eq!(
            encode_tombstone_attempt_ids(&over_limit)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );

        let maximum_ascii_entries = (0..MAX_ATTEMPTS_PER_TURN)
            .map(|index| {
                Map::from_iter([
                    (
                        "attemptId".to_owned(),
                        Value::String(format!("attempt-{index:03}-{}", "a".repeat(112))),
                    ),
                    ("conversationId".to_owned(), Value::String("c".repeat(256))),
                    ("turnId".to_owned(), Value::String("t".repeat(128))),
                    ("taskId".to_owned(), Value::String("k".repeat(256))),
                    ("createdAt".to_owned(), Value::from(u64::MAX)),
                    ("effectiveAt".to_owned(), Value::from(u64::MAX)),
                    (
                        "dispatchMode".to_owned(),
                        Value::String("conversation_executor".to_owned()),
                    ),
                ])
            })
            .collect::<Vec<_>>();
        let maximum_ascii_directory = serde_json::to_vec(&maximum_ascii_entries).unwrap();
        assert!(maximum_ascii_directory.len() <= MAX_ATTEMPT_TURN_DIRECTORY_BYTES);
        assert_eq!(
            decode_attempt_turn_directory(&maximum_ascii_directory)
                .unwrap()
                .len(),
            MAX_ATTEMPTS_PER_TURN
        );

        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            load_attempt_identity_ids(
                &database,
                &mut transaction,
                "legacy-conversation",
                "legacy-turn",
                Some("legacy-attempt"),
            )
            .unwrap(),
            vec!["legacy-attempt"]
        );
        let entries = (0..=MAX_ATTEMPTS_PER_TURN)
            .map(|_| Map::new())
            .collect::<Vec<_>>();
        assert_eq!(
            store_attempt_turn_directory(
                &database,
                &mut transaction,
                "conversation",
                "turn",
                &entries,
            )
            .unwrap_err()
            .kind(),
            io::ErrorKind::OutOfMemory
        );
    }

    #[test]
    fn turn_delete_cascades_every_historical_attempt_and_releases_all_claims_after_retention() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "cascade-conversation".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"settled"}"#.to_vec(),
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "cascade-first-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "cascade-turn".to_owned(),
                attempt_id: Some("cascade-first".to_owned()),
                created_at_ms: 10,
                committed_at_ms: 10,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Cascade".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 10,
                },
            },
        )
        .unwrap();
        append_historical_attempt(
            &database,
            &mut seed,
            "cascade-conversation",
            "cascade-turn",
            "cascade-first",
            "cascade-second",
            20,
        );
        database.commit(seed).unwrap();

        let mut remove = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        delete(
            &database,
            &mut remove,
            "cascade-conversation",
            &["cascade-turn".to_owned()],
            100,
        )
        .unwrap();
        database.commit(remove).unwrap();

        let mut read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert!(attempt_get(&database, &mut read, "cascade-first")
            .unwrap()
            .is_none());
        assert!(attempt_get(&database, &mut read, "cascade-second")
            .unwrap()
            .is_none());
        assert!(
            events_list(&database, &mut read, "cascade-second", 0, 10, true)
                .unwrap()
                .is_none()
        );
        assert_eq!(
            serde_json::from_slice::<Value>(
                &timing_trace_list(&database, &mut read, "cascade-conversation", None, 10,)
                    .unwrap(),
            )
            .unwrap(),
            json!({"records": [], "has_more": false})
        );
        for attempt_id in ["cascade-first", "cascade-second"] {
            let claim_key =
                global_identity_claim_key(&read, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id).unwrap();
            assert!(database
                .entity_get(&mut read, &claim_key)
                .unwrap()
                .is_some());
        }
        drop(read);

        let mut prune = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            prune_expired_tombstones(&database, &mut prune, TOMBSTONE_RETENTION_MS + 101).unwrap(),
            1
        );
        database.commit(prune).unwrap();
        let mut final_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for (namespace, identity) in [
            (TURN_ID_CLAIM_NAMESPACE, "cascade-turn"),
            (ATTEMPT_ID_CLAIM_NAMESPACE, "cascade-first"),
            (ATTEMPT_ID_CLAIM_NAMESPACE, "cascade-second"),
        ] {
            let claim_key = global_identity_claim_key(&final_read, namespace, identity).unwrap();
            assert!(database
                .entity_get(&mut final_read, &claim_key)
                .unwrap()
                .is_none());
        }
    }

    #[test]
    fn restored_conversation_retains_inert_history_for_complete_purge() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        append_settled(
            &database,
            &mut seed,
            &AppendSettledRequest {
                conversation_id: "restore-history".to_owned(),
                actor: "assistant".to_owned(),
                status: "completed".to_owned(),
                projection_json: br#"{"content":"settled"}"#.to_vec(),
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: "restore-first-command".to_owned(),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: "restore-turn".to_owned(),
                attempt_id: Some("restore-first".to_owned()),
                created_at_ms: 10,
                committed_at_ms: 10,
                defaults: TurnDefaults {
                    allow_create: true,
                    title: "Restore history".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 10,
                },
            },
        )
        .unwrap();
        append_historical_attempt(
            &database,
            &mut seed,
            "restore-history",
            "restore-turn",
            "restore-first",
            "restore-second",
            20,
        );
        database.commit(seed).unwrap();

        let mut trash = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::delete(
            &database,
            &mut trash,
            &crate::conversation_header::DeleteRequest {
                conversation_id: "restore-history".to_owned(),
                deleted_at_ms: 100,
            },
        )
        .unwrap();
        database.commit(trash).unwrap();
        let mut restore = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::restore(
            &database,
            &mut restore,
            &crate::conversation_header::RestoreRequest {
                conversation_id: "restore-history".to_owned(),
                committed_at_ms: 200,
            },
        )
        .unwrap();
        database.commit(restore).unwrap();

        let mut inspect = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        assert_eq!(
            load_attempt_turn_directory(
                &database,
                &mut inspect,
                "restore-history",
                "restore-turn",
                Some("restore-first"),
            )
            .unwrap()
            .len(),
            2
        );
        for attempt_id in ["restore-first", "restore-second"] {
            assert!(attempt_get(&database, &mut inspect, attempt_id)
                .unwrap()
                .is_none());
        }
        drop(inspect);

        let mut purge = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::conversation_header::purge(
            &database,
            &mut purge,
            &crate::conversation_header::PurgeRequest {
                conversation_id: "restore-history".to_owned(),
                purged_at_ms: 300,
            },
        )
        .unwrap();
        database.commit(purge).unwrap();
        let mut final_read = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        for attempt_id in ["restore-first", "restore-second"] {
            let claim_key =
                global_identity_claim_key(&final_read, ATTEMPT_ID_CLAIM_NAMESPACE, attempt_id)
                    .unwrap();
            assert!(database
                .entity_get(&mut final_read, &claim_key)
                .unwrap()
                .is_none());
        }
    }
}
