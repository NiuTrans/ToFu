//! Canonical owner-scoped Turn authority and ordered transcript indexes.
//!
//! Turn documents may overflow to blobs, while small entity records keep lane
//! allocation, ordered reads, search invalidation, and sync replay atomic with
//! the conversation header and command receipt.

pub(crate) mod migration;

#[path = "turn/attempt_event_migration.rs"]
pub(crate) mod attempt_event_migration;
#[path = "turn/attempt_migration.rs"]
pub(crate) mod attempt_migration;
#[path = "turn/sync_event_migration.rs"]
pub(crate) mod sync_event_migration;
#[path = "turn/sync_head_migration.rs"]
pub(crate) mod sync_head_migration;

#[path = "conversation_diagnostic.rs"]
pub(crate) mod diagnostic;
pub(crate) mod model_context;
pub(crate) mod event_retirement;

#[path = "turn/snapshot_phase.rs"]
mod snapshot_phase;

#[path = "turn_effect_interruption.rs"]
mod effect_interruption;

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::io;

use serde::Serialize;
use serde_json::value::RawValue;
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
    ATTEMPT_RECOVERY_INDEX_NAMESPACE, ATTEMPT_SUPERSEDED_INDEX_NAMESPACE,
    ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE, ATTEMPT_TIMING_TASK_INDEX_NAMESPACE,
    ATTEMPT_TURN_COUNT_NAMESPACE, ATTEMPT_TURN_DIRECTORY_NAMESPACE,
    CONVERSATION_EXECUTION_EPOCH_NAMESPACE, CONVERSATION_SYNC_AGE_INDEX_NAMESPACE,
    CONVERSATION_SYNC_EVENT_NAMESPACE, CONVERSATION_SYNC_HEAD_NAMESPACE,
    GENERATION_ATTEMPT_NAMESPACE, MAX_ATTEMPTS_PER_TURN, MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
    MAX_ATTEMPT_EVENT_PAGE_BYTES, MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION,
    MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES, MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION,
    MAX_ATTEMPT_TOMBSTONE_BYTES, MAX_ATTEMPT_TURN_DIRECTORY_BYTES,
    MAX_CLEANUP_INDEX_SCAN_ROWS_PER_TRANSACTION, MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY,
    MAX_RECOVERY_INDEX_ROWS_PER_OWNER, MAX_SYNC_PRUNE_MATERIALIZED_BYTES,
    MAX_SYNC_PRUNE_ROWS_PER_TRANSACTION, MAX_TIMING_TRACE_CLIENT_OBSERVATIONS,
    MAX_TIMING_TRACE_COUNTER, MAX_TIMING_TRACE_PERSISTED_BYTES, MAX_TIMING_TRACE_ROWS_PER_QUERY,
    MAX_TIMING_TRACE_TASK_CANDIDATES, MAX_LEGACY_DOCUMENT_BYTES,
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

mod text_tail;
pub(crate) mod execution_frame;
mod sync_attempt_reference;

#[path = "turn/agent_machine_tokens.generated.rs"]
mod agent_machine_tokens;
const INDEX_PAGE_ROWS: usize = 1_000;
const MAX_LIST_ROWS: usize = 10_000;
const MAX_SCAN_PAGES: usize = 11;
const MAX_DELTA_ROWS: usize = 2_000;
const MAX_DELETE_ROWS: usize = 2_000;
const MAX_CLONE_ROWS: usize = 2_000;
pub(crate) const MAX_RELATED_TURNS_PER_ANNOUNCEMENT: usize = 2_000;
pub(crate) const MAX_VISIBLE_TURNS_PER_SYNCHRONIZATION: usize = 2_000;
/// Absolute visible-phase window bound, mirroring the SQL sidecar's
/// `offset + len(messages) > 4096` fence and the executor's
/// `visible_phase_identity` [0, 4096) index domain.
pub(crate) const MAX_VISIBLE_PHASE_INDEX: u64 = 4_096;
const MAX_DELETE_BRANCH_LANES: usize = 256;
const MAX_TOMBSTONE_PRUNE_ROWS: usize = 256;
const SEARCH_PROJECTION_PAGE_ROWS: usize = crate::generated_conversation_search::SOURCE_PAGE_ROWS;
const SEARCH_PROJECTION_PAGE_BYTES: u64 =
    crate::generated_conversation_search::SOURCE_PAGE_BYTES as u64;
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
pub(crate) struct SearchProjectionPage {
    pub turns: Vec<crate::turn_search_projection::TurnSearchDocument>,
    pub next_cursor: Vec<u8>,
    pub complete: bool,
    pub skipped_oversized: usize,
    pub source_bytes: u64,
    pub source_turns: usize,
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
    /// Pre-extracted `projection.timestamp` from the compiler's raw-map
    /// pass; keeps the hot path free of a full typed projection parse.
    pub projection_timestamp: Option<Value>,
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
    pub expected_parent_projection_revision: Option<u64>,
    pub require_parent_not_stopped: bool,
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
    pub live: bool,
    pub start_index: u64,
    pub updated_at_ms: u64,
    pub committed_at_ms: u64,
}

pub(crate) struct ProjectionUpdateRequest {
    pub preserve_activity: bool,
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

/// Resume may refresh only bounded display context, never submitted content.
/// Shared by semantic admission and direct transaction-IR validation.
pub(crate) fn valid_resume_input_update(update: &Map<String, Value>) -> bool {
    update.len() == 1
        && match update.get("contextSnapshot") {
            Some(Value::Null) => true,
            Some(Value::Object(context)) => context
                .get("snapshot")
                .and_then(Value::as_object)
                .is_some_and(|snapshot| {
                    serde_json::to_vec(snapshot)
                        .map(|bytes| bytes.len() <= 16 * 1024)
                        .unwrap_or(false)
                }),
            _ => false,
        }
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
pub struct CleanupRequest {
    pub retention_ms: u64,
    pub limit: usize,
    pub now_ms: u64,
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
    pub model_continuation: Value,
    pub attempt_id: String,
    pub task_id: String,
    pub terminal: bool,
    pub status: String,
    pub slim: bool,
    pub content: String,
    pub thinking: String,
    pub projection: Value,
    pub projection_patch: Option<Map<String, Value>>,
    pub projection_segments_stable: bool,
    pub settlement: Map<String, Value>,
    pub error: Map<String, Value>,
    pub event_type: String,
    pub event_payload: Map<String, Value>,
    pub background_command_ack: Option<Value>,
    pub task_event: Option<crate::transaction_ir::IndexedStreamAppendItem>,
    pub preceding_task_events: Vec<crate::transaction_ir::IndexedStreamAppendItem>,
    pub flow_root_attempt_id: String,
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
    verified_stable_segments: Option<execution_frame::VerifiedStableSegments>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ProjectionHeadDescriptor {
    head_id: String,
    attempt_id: String,
    base_revision: u64,
    patch_count: usize,
    patch_bytes: usize,
    text_tail: Option<text_tail::TextTail>,
    frame_witness: Option<execution_frame::ProjectionWitness>,
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
        TURN_LANE_HEAD_NAMESPACE,
        &prefix,
    )?;
    let rows = database.entity_scan(transaction, &start, &end, MAX_DELETE_BRANCH_LANES + 1)?;
    if rows.len() > MAX_DELETE_BRANCH_LANES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "conversation lane count exceeds 256",
        ));
    }
    rows.into_iter().try_fold(0_u64, |total, (key, value)| {
        let state = decode_lane_state(&value, "turn lane state is malformed")?;
        let count = if let Some(count) = state.count {
            count
        } else {
            // Legacy head-only row: read its historical count key. The key
            // layout is [u16 conversation][u16 lane]; parse the lane segment.
            let key_bytes = key.key_bytes();
            let malformed = |_| invalid_data("turn lane state key is malformed");
            if key_bytes.len() < 2 {
                return Err(malformed(()));
            }
            let conv_len = u16::from_be_bytes(key_bytes[..2].try_into().unwrap()) as usize;
            let rest = key_bytes.get(2 + conv_len..).ok_or_else(|| malformed(()))?;
            if rest.len() < 2 {
                return Err(malformed(()));
            }
            let lane_len = u16::from_be_bytes(rest[..2].try_into().unwrap()) as usize;
            let lane_bytes = rest.get(2..2 + lane_len).ok_or_else(|| malformed(()))?;
            let lane_id = std::str::from_utf8(lane_bytes).map_err(|_| malformed(()))?;
            decode_u64(
                database.entity_get(
                    transaction,
                    &lane_count_key(transaction, conversation_id, lane_id)?,
                )?,
                "turn lane count is malformed",
            )?
        };
        total
            .checked_add(count)
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

/// Fenced standalone event appends (`require_attempts_live`) prove inside
/// the append transaction that every listed attempt still exists with a
/// live status — the native half of the SQL sidecar fence in
/// `_records._append_event_row`. A missing attempt fails closed.
pub(crate) fn attempts_all_live(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_ids: &[String],
) -> io::Result<bool> {
    for attempt_id in attempt_ids {
        let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? else {
            return Ok(false);
        };
        if !attempt_is_live(&loaded.document) {
            return Ok(false);
        }
    }
    Ok(true)
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
            ));
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

pub(crate) fn attempt_event_head_key(
    transaction: &AuthorityTransaction,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    entity_key(
        transaction,
        ATTEMPT_EVENT_HEAD_NAMESPACE,
        attempt_id.as_bytes(),
    )
}

pub(crate) fn attempt_event_key(
    transaction: &AuthorityTransaction,
    attempt_id: &str,
    sequence: u64,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(attempt_id.len() + 10);
    push_text_key(&mut encoded, attempt_id)?;
    encoded.extend_from_slice(&sequence.to_be_bytes());
    entity_key(transaction, ATTEMPT_EVENT_NAMESPACE, &encoded)
}

pub(crate) fn attempt_event_prefix(attempt_id: &str) -> io::Result<Vec<u8>> {
    let mut encoded = Vec::with_capacity(attempt_id.len() + 2);
    push_text_key(&mut encoded, attempt_id)?;
    Ok(encoded)
}

pub(crate) fn attempt_event_retention_index_key(
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

pub(crate) fn attempt_event_retention_index_value(
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

/// Replace a visible lane entry or insert into its existing order. Every
/// derived transcript page belongs to the old lane contents until this same
/// transaction invalidates it. Fresh tail appends use their separate owner.
fn store_changed_lane_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    ordinal: u64,
    stored: Vec<u8>,
) -> io::Result<()> {
    if lane_id == "main" {
        crate::transcript_archive::invalidate(database, transaction, conversation_id)?;
    }
    database.entity_put(transaction,
        lane_index_key(transaction, conversation_id, lane_id, ordinal)?, stored)
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

pub(crate) fn model_context_revision(document: &Map<String, Value>) -> io::Result<u64> {
    let revision = document.get("projectionRevision").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("Turn projection revision is malformed"))?;
    let offset = match document.get("_modelContextDisplayOffset") {
        None => 0,
        Some(value) => value.as_u64()
            .ok_or_else(|| invalid_data("Turn context display offset is malformed"))?,
    };
    revision.checked_sub(offset)
        .ok_or_else(|| invalid_data("Turn context display offset exceeds its revision"))
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
    let mut header = json!({
        "actor": text("actor")?,
        "turnId": text("turnId")?,
        "parentTurnId": optional_text("parentTurnId")?,
        "status": text("status")?,
        "currentAttemptId": optional_text("currentAttemptId")?,
        "projectionRevision": unsigned("projectionRevision")?,
        "updatedAt": unsigned("updatedAt")?,
    });
    // Actor belongs to this existing compact head, not a separately maintained
    // count. Context readers use projectionRevision without a display offset.
    if document.contains_key("_modelContextDisplayOffset") {
        header["modelContextRevision"] = json!(model_context_revision(document)?);
    }
    serde_json::to_vec(&header)
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
    let key = lane_compaction_index_key(transaction, conversation_id, lane_id, ordinal)?;
    let value = lane_compaction_index_value(document)?;
    model_context::before_header_write(database, transaction,
        conversation_id, lane_id, ordinal, &key, &value)?;
    database.entity_put(transaction, key, value)
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
    let lane_state = read_lane_state(database, transaction, conversation_id, lane_id)?;
    let expected_count = lane_count_of(
        database,
        transaction,
        conversation_id,
        lane_id,
        lane_state.as_ref(),
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

fn attempt_superseded_index_key(
    transaction: &AuthorityTransaction,
    superseded_at_ms: u64,
    conversation_id: &str,
    attempt_id: &str,
) -> io::Result<EntityKey> {
    let mut encoded = Vec::with_capacity(12 + conversation_id.len() + attempt_id.len());
    encoded.extend_from_slice(&superseded_at_ms.to_be_bytes());
    push_text_key(&mut encoded, conversation_id)?;
    push_text_key(&mut encoded, attempt_id)?;
    entity_key(transaction, ATTEMPT_SUPERSEDED_INDEX_NAMESPACE, &encoded)
}

fn decode_superseded_index_key(key: &EntityKey) -> io::Result<(u64, String, String)> {
    let encoded = key.key_bytes();
    if encoded.len() < 12 {
        return Err(invalid_data("attempt superseded index key is malformed"));
    }
    let superseded_at_ms = u64::from_be_bytes(encoded[..8].try_into().unwrap());
    let mut offset = 8;
    let read_text = |offset: &mut usize| -> io::Result<String> {
        let length_end = offset
            .checked_add(2)
            .filter(|end| *end <= encoded.len())
            .ok_or_else(|| invalid_data("attempt superseded index key is malformed"))?;
        let length = u16::from_be_bytes(encoded[*offset..length_end].try_into().unwrap()) as usize;
        *offset = length_end;
        let value_end = offset
            .checked_add(length)
            .filter(|end| *end <= encoded.len())
            .ok_or_else(|| invalid_data("attempt superseded index key is malformed"))?;
        let value = std::str::from_utf8(&encoded[*offset..value_end])
            .map(str::to_owned)
            .map_err(|_| invalid_data("attempt superseded index identity is not UTF-8"))?;
        *offset = value_end;
        Ok(value)
    };
    let conversation_id = read_text(&mut offset)?;
    let attempt_id = read_text(&mut offset)?;
    if offset != encoded.len() || conversation_id.is_empty() || attempt_id.is_empty() {
        return Err(invalid_data("attempt superseded index key is malformed"));
    }
    Ok((superseded_at_ms, conversation_id, attempt_id))
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

pub(crate) fn after_key(
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

/// Fused per-lane bookkeeping (2026-09-20, turn-append hot path): the lane
/// head ordinal, the exact lane count, and the latest-appended turn identity
/// live in ONE lane-state record at the historical `TURN_LANE_HEAD_NAMESPACE`
/// key. The append path used to pay three witness-verified descents (head
/// get, count get, predecessor reverse-scan) plus two counter writes per
/// turn; the fused record makes it one read plus one write. Legacy rows
/// (bare 8-byte head u64) decode transparently: count and predecessor fall
/// back to the legacy count key / lane-index reverse scan exactly once per
/// lane, and the next append rewrites the record in the fused format.
/// Deletion keeps the record with an exact count of zero so the head stays
/// monotonic across deletion gaps, and clears `last_turn_id` when the lane
/// tail itself is deleted (the next append's one legacy rescan re-seeds it).
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct LaneState {
    pub(crate) head: u64,
    pub(crate) count: Option<u64>,
    pub(crate) last_turn_id: Option<String>,
}

const LANE_STATE_MARKER: u8 = 0xFF;

fn encode_lane_state(state: &LaneState) -> Vec<u8> {
    let last = state.last_turn_id.as_deref().unwrap_or("");
    let mut out = Vec::with_capacity(19 + last.len());
    out.push(LANE_STATE_MARKER);
    out.extend_from_slice(&state.head.to_le_bytes());
    out.extend_from_slice(&state.count.unwrap_or(0).to_le_bytes());
    out.extend_from_slice(&(last.len() as u16).to_le_bytes());
    out.extend_from_slice(last.as_bytes());
    out
}

fn decode_lane_state(value: &[u8], description: &str) -> io::Result<LaneState> {
    if value.len() == 8 {
        return Ok(LaneState {
            head: u64::from_le_bytes(value.try_into().unwrap()),
            count: None,
            last_turn_id: None,
        });
    }
    if value.len() >= 19 && value[0] == LANE_STATE_MARKER {
        let head = u64::from_le_bytes(value[1..9].try_into().unwrap());
        let count = u64::from_le_bytes(value[9..17].try_into().unwrap());
        let last_len = u16::from_le_bytes(value[17..19].try_into().unwrap()) as usize;
        if value.len() != 19 + last_len {
            return Err(invalid_data(description));
        }
        let last_turn_id = if last_len == 0 {
            None
        } else {
            Some(
                std::str::from_utf8(&value[19..])
                    .map_err(|_| invalid_data(description))?
                    .to_owned(),
            )
        };
        return Ok(LaneState {
            head,
            count: Some(count),
            last_turn_id,
        });
    }
    Err(invalid_data(description))
}

fn lane_state_key(
    transaction: &AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<EntityKey> {
    entity_key(
        transaction,
        TURN_LANE_HEAD_NAMESPACE,
        &lane_prefix(conversation_id, lane_id)?,
    )
}

pub(crate) fn read_lane_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<Option<LaneState>> {
    let key = lane_state_key(transaction, conversation_id, lane_id)?;
    let Some(value) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    Ok(Some(decode_lane_state(
        &value,
        "turn lane state is malformed",
    )?))
}

/// Exact lane count with the legacy fallback: a fused record carries it;
/// every other shape reads the historical count key (absent means zero,
/// exactly like `decode_u64`).
fn lane_count_of(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    state: Option<&LaneState>,
) -> io::Result<u64> {
    if let Some(count) = state.and_then(|state| state.count) {
        return Ok(count);
    }
    decode_u64(
        database.entity_get(
            transaction,
            &lane_count_key(transaction, conversation_id, lane_id)?,
        )?,
        "turn lane count is malformed",
    )
}

/// Latest appended turn identity on the lane, with the legacy fallback: a
/// fused record carries it; a legacy row (or a cleared tail) pays one
/// lane-index reverse scan, and the next append rewrites the fused record.
fn lane_last_turn_id(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    state: Option<&LaneState>,
) -> io::Result<Option<String>> {
    let Some(state) = state else {
        return Ok(None);
    };
    if let Some(last) = &state.last_turn_id {
        return Ok(Some(last.clone()));
    }
    // The reverse scan ends BELOW the incoming turn's ordinal (head + 1),
    // exactly like the pre-fusion path — ending at the current head would
    // skip the actual tail entry (2026-09-20 fusion follow-up).
    let state_head = state
        .head
        .checked_add(1)
        .ok_or_else(|| invalid_data("turn lane ordinal overflow"))?;
    let prefix = lane_prefix(conversation_id, lane_id)?;
    let (start, _) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    let end = lane_index_key(transaction, conversation_id, lane_id, state_head)?;
    let Some((_, previous)) = database
        .entity_scan_reverse(transaction, &start, &end, 1)?
        .pop()
    else {
        return Ok(None);
    };
    let logical_key =
        crate::versioned_document::stored_document_logical_key(&previous, DOCUMENT_IDENTITY)?;
    let prefix = format!("{}\u{1f}", conversation_id);
    Ok(Some(
        logical_key
            .strip_prefix(prefix.as_str())
            .filter(|turn_id| !turn_id.is_empty())
            .ok_or_else(|| invalid_data("turn predecessor identity is malformed"))?
            .to_owned(),
    ))
}

fn decode_turn_value(bytes: &[u8]) -> io::Result<Map<String, Value>> {
    match serde_json::from_slice::<Value>(bytes) {
        Ok(Value::Object(document)) => Ok(document),
        _ => Err(invalid_data("turn document is malformed")),
    }
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
    let text_tail = raw.get("textTail").map(text_tail::TextTail::decode).transpose()?;
    let frame_witness = match raw.get("frameWitness") {
        Some(raw) => execution_frame::ProjectionWitness::decode(raw,
            document.get("projectionRevision").and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("Turn frame revision is malformed"))?)?,
        None => None,
    };
    if frame_witness.is_some() && text_tail.is_some() {
        return Err(invalid_data("Turn frame witness cannot cross a text tail"));
    }
    let expected_revision = base_revision.checked_add(patch_count as u64)
        .and_then(|value| value.checked_add(text_tail.as_ref().map_or(0, |tail| tail.revisions)));
    if (patch_count == 0) != (patch_bytes == 0)
        || !matches!(
            document.get("status").and_then(Value::as_str),
            Some("pending" | "running")
        )
        || document.get("currentAttemptId").and_then(Value::as_str) != Some(&attempt_id)
        || document.get("projectionRevision").and_then(Value::as_u64)
            != expected_revision
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
        text_tail,
        frame_witness,
    }))
}

fn projection_head_value(head: &ProjectionHeadDescriptor) -> Value {
    let mut value = json!({
        "headId": head.head_id,
        "attemptId": head.attempt_id,
        "baseRevision": head.base_revision,
        "patchCount": head.patch_count,
        "patchBytes": head.patch_bytes,
    });
    if let Some(tail) = &head.text_tail {
        value["textTail"] = serde_json::to_value(tail).expect("bounded text descriptor is JSON safe");
    }
    if let Some(witness) = &head.frame_witness {
        value["frameWitness"] = serde_json::to_value(witness)
            .expect("bounded frame witness is JSON safe");
    }
    value
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
    crate::versioned_document::put_with_blob_owner_bounded(
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
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
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

/// Single-event cleanup only. Bulk recovery keeps structural retirement so
/// hundreds of heads cannot expand past the transaction's point-write budget.
fn retire_event_projection_head(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    head: Option<&ProjectionHeadDescriptor>,
) -> io::Result<()> {
    if let Some(head) = head {
        let (start, end) =
            projection_head_range(transaction, conversation_id, turn_id, &head.head_id)?;
        // A normal head is one checkpoint plus at most 64 patches. Structural
        // range retirement drains the authority-wide overlay before committing,
        // coupling each settled Turn to every other conversation's pending work.
        // Exact tombstones keep bounded cleanup in the same ordinary WAL delta.
        // Scan one extra row: unexpected larger ranges retain the structural
        // fallback, and the scan witness preserves concurrent-insert conflicts.
        let text_pages = head.text_tail.as_ref().map_or(0, |tail| tail.page_count());
        let maximum_rows = MAX_TURN_PROJECTION_HEAD_PATCHES + 1 + text_pages;
        let rows = database.entity_scan(transaction, &start, &end, maximum_rows + 1)?;
        if rows.len() > maximum_rows {
            database.entity_retire_range(transaction, &start, &end)?;
        } else {
            for (key, _) in rows {
                database.entity_delete(transaction, key)?;
            }
        }
    }
    Ok(())
}

fn materialize_projection_head(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    document: &mut Map<String, Value>,
    head: &ProjectionHeadDescriptor,
) -> io::Result<Option<execution_frame::VerifiedStableSegments>> {
    let _head_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PROJECTION_HEAD_MATERIALIZE);
    let checkpoint_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PROJECTION_HEAD_CHECKPOINT);
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
    let (_, checkpoint_value, checkpoint_source_bytes) = crate::versioned_document::materialize_stored_document_value_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &checkpoint_stored,
        PROJECTION_HEAD_DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    crate::fsync_stats::note_projection_checkpoint_bytes(checkpoint_source_bytes);
    let Value::Object(mut checkpoint) = checkpoint_value else {
        return Err(invalid_data("Turn projection checkpoint is malformed"));
    };
    if checkpoint.get("attemptId").and_then(Value::as_str) != Some(&head.attempt_id)
        || checkpoint.get("revision").and_then(Value::as_u64) != Some(head.base_revision)
    {
        return Err(invalid_data("Turn projection checkpoint fence differs"));
    }
    let Some(Value::Object(mut projection)) = checkpoint.remove("projection") else {
        return Err(invalid_data("Turn projection checkpoint value is malformed"));
    };
    drop(checkpoint_timer);
    let mut observed_patch_bytes = 0_usize;
    for offset in 1..=head.patch_count {
        let load_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PROJECTION_HEAD_PATCH_LOAD);
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
        let (_, patch_value, patch_source_bytes) = crate::versioned_document::materialize_stored_document_value_bounded(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &patch_stored,
            PROJECTION_HEAD_DOCUMENT_IDENTITY,
            crate::generated_tofudb_ir::MAX_LEGACY_DOCUMENT_BYTES,
        )?;
        crate::fsync_stats::note_projection_patch_bytes(patch_source_bytes);
        let Value::Object(envelope) = patch_value else {
            return Err(invalid_data("Turn projection patch envelope is malformed"));
        };
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
        drop(load_timer);
        let _apply_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PROJECTION_HEAD_PATCH_APPLY);
        projection = crate::turn_projection_patch::apply_projection_patch_owned(projection, patch)
            .map_err(|_| invalid_data("Turn projection patch chain is invalid"))?;
        crate::fsync_stats::note_projection_patch_applied();
    }
    if observed_patch_bytes != head.patch_bytes {
        return Err(invalid_data("Turn projection patch byte witness differs"));
    }
    text_tail::materialize(database, transaction, conversation_id, turn_id, head, &mut projection)?;
    let verified_stable_segments = if let Some(witness) = &head.frame_witness {
        witness.verify(&projection,
            document.get("actor").and_then(Value::as_str).unwrap_or("assistant"),
            document.get("status").and_then(Value::as_str).unwrap_or("running"))?
    } else { None };
    document.insert("projection".to_owned(), Value::Object(projection));
    document.remove("_projectionHead");
    Ok(verified_stable_segments)
}

fn materialize_turn(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    stored: &[u8],
) -> io::Result<Map<String, Value>> {
    // The bounded scoped reader transfers its single parsed value; Turn
    // reconstruction owns that object and does not rebuild or clone its tree.
    let (_, value, _) = crate::versioned_document::materialize_stored_document_value_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let Value::Object(mut document) = value else {
        return Err(invalid_data("turn document is malformed"));
    };
    if let Some(head) = projection_head_from_document(&document)? {
        materialize_projection_head(database, transaction, &mut document, &head)?;
    }
    Ok(document)
}

fn encode_response(value: &impl Serialize) -> io::Result<Vec<u8>> {
    let encoded =
        serde_json::to_vec(value).map_err(|_| invalid_data("turn response cannot be encoded"))?;
    if encoded.len() > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn response exceeds its bounded streamed payload",
        ));
    }
    Ok(encoded)
}

/// Normalize one settlement map in place: overwrite the derived scalars and
/// fill the defaulted keys without allocating throwaway key strings when the
/// keys are already present (the common settled-turn case). The resulting
/// map is identical to the previous remove/rebuild pass.
fn normalize_settlement_in_place(settlement: &mut Map<String, Value>, status: &str) {
    // Canonical terminal outcomes have static spellings. Preserve the existing
    // owned strings on the common normalized path instead of replacing them.
    let outcome = match settlement.get("outcome").and_then(Value::as_str)
        .unwrap_or(if status == "superseded" { "interrupted" } else { status }) {
        "completed" => "completed",
        "interrupted" => "interrupted",
        "truncated" => "truncated",
        _ => "failed",
    };
    if settlement.get("outcome").and_then(Value::as_str) != Some(outcome) {
        settlement.insert("outcome".to_owned(), Value::String(outcome.to_owned()));
    }
    if settlement.get("cause").and_then(Value::as_str).is_none() {
        settlement.insert("cause".to_owned(), Value::String("ingested".to_owned()));
    }
    for key in ["providerFinishReason", "error", "streamState"] {
        if !settlement.contains_key(key) { settlement.insert(key.to_owned(), Value::Null); }
    }
    if !settlement.contains_key("resumeOptions") {
        settlement.insert("resumeOptions".to_owned(), json!([]));
    }
    if !settlement.contains_key("evidence") {
        let cause = settlement.get("cause").and_then(Value::as_str).unwrap_or("ingested");
        let evidence = match (outcome, cause) {
            (_, "server_restart" | "conversation_deleted") => "system_recovery",
            ("truncated", _) => "provider_limit",
            ("failed", _) => "generation_error",
            ("completed", "provider_finished") => "legacy_finish",
            _ => "external_authority",
        };
        settlement.insert("evidence".to_owned(), Value::String(evidence.to_owned()));
    }
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
    let _profile = crate::request_profile::Probe::new(crate::request_profile::TURN_PUBLIC);

    let context_revision = model_context_revision(&document)?;
    document.remove("_modelContextDisplayOffset");
    if let Some(slot) = document.get_mut("modelContextRevision") {
        *slot = Value::from(context_revision);
    } else {
        document.insert("modelContextRevision".to_owned(), Value::from(context_revision));
    }
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
    let status = document.get("status").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn status is malformed"))?;
    let active = matches!(status, "pending" | "running");
    let terminal = matches!(status, "completed" | "interrupted" | "truncated" | "failed" | "superseded");
    // Unknown statuses retain their original public spelling, while their
    // missing/invalid terminal settlement still normalizes to failed.
    let default_outcome = match status {
        "completed" => "completed",
        "interrupted" | "superseded" => "interrupted",
        "truncated" => "truncated",
        _ => "failed",
    };
    let settlement = match document.get_mut("settlement") {
        Some(Value::Object(settlement)) => settlement,
        _ => return Err(invalid_data("turn settlement is malformed")),
    };
    if !active { normalize_settlement_in_place(settlement, default_outcome); }
    let public_status = if terminal {
        match settlement.get("outcome").and_then(Value::as_str) {
            Some("completed") => Some("completed"),
            Some("interrupted") => Some("interrupted"),
            Some("truncated") => Some("truncated"),
            Some("failed") => Some("failed"),
            _ => None,
        }
    } else { None };
    if let Some(status) = public_status {
        if document.get("status").and_then(Value::as_str) != Some(status) {
            document.insert("status".to_owned(), Value::String(status.to_owned()));
        }
    }
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
    transaction.note_conversation_commit(conversation_id, sync_sequence)
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

/// Serialize-once payload shape for `turn.upsert` sync events. Large turns
/// ride as `&RawValue` byte references, so the event serialization never
/// re-escapes content the caller already encoded: byte-for-byte identical
/// to the previous `json!` + `to_vec` construction (pinned by
/// `raw_turn_upsert_encoding_matches_the_value_path_byte_for_byte`).
#[derive(Serialize)]
struct TurnUpsertPayload<'a> {
    attempts: Vec<&'a Value>,
    #[serde(rename = "conversationRevision")]
    conversation_revision: u64,
    #[serde(rename = "queueItemUpserts", skip_serializing_if = "Vec::is_empty")]
    queue_item_upserts: Vec<Value>,
    #[serde(rename = "removedQueueIds", skip_serializing_if = "Vec::is_empty")]
    removed_queue_ids: Vec<String>,
    turns: Vec<&'a RawValue>,
}

/// Reference shape for the settled-append response. Production assembles
/// the same bytes by splicing the raw turn (`append_settled_response_bytes`);
/// this derive remains as the byte-exactness authority pinned by
/// `settled_response_splice_matches_the_derive_encoding_byte_for_byte` and
/// `raw_response_encodings_match_the_value_path_byte_for_byte`.
#[allow(dead_code)]
#[derive(Serialize)]
struct AppendSettledResponse<'a> {
    attempt: &'a Option<Value>,
    #[serde(rename = "conversationRevision")]
    conversation_revision: u64,
    turn: &'a RawValue,
}

#[derive(Serialize)]
struct CreatePairResponse<'a> {
    #[serde(rename = "_needsStart")]
    needs_start: bool,
    attempt: &'a Value,
    #[serde(rename = "conversationRevision")]
    conversation_revision: u64,
    #[serde(rename = "idempotentReplay")]
    idempotent_replay: bool,
    #[serde(rename = "position", skip_serializing_if = "Option::is_none")]
    position: Option<Value>,
    #[serde(rename = "queueId", skip_serializing_if = "Option::is_none")]
    queue_id: Option<String>,
    #[serde(rename = "queueItem", skip_serializing_if = "Option::is_none")]
    queue_item: Option<Value>,
    #[serde(rename = "queued", skip_serializing_if = "std::ops::Not::not")]
    queued: bool,
    #[serde(rename = "streamCursor")]
    stream_cursor: u64,
    #[serde(rename = "submittedTurn")]
    submitted_turn: &'a RawValue,
    turn: &'a RawValue,
}

#[derive(Serialize)]
struct QueueResumeResponse<'a> {
    #[serde(rename = "_needsStart")]
    needs_start: bool,
    attempt: &'a Value,
    #[serde(rename = "conversationRevision")]
    conversation_revision: u64,
    #[serde(rename = "idempotentReplay")]
    idempotent_replay: bool,
    #[serde(rename = "queueId")]
    queue_id: &'a str,
    queued: bool,
    #[serde(rename = "streamCursor")]
    stream_cursor: u64,
    #[serde(rename = "submittedTurn")]
    submitted_turn: &'a RawValue,
    turn: &'a RawValue,
}

fn encode_response_raw(value: &impl Serialize) -> io::Result<Vec<u8>> {
    serde_json::to_vec(value).map_err(|_| invalid_data("response cannot be encoded"))
}

/// Serializes a document map in serde_json's canonical sorted-key order,
/// splicing the already-encoded projection bytes in place instead of
/// re-escaping them. Byte-for-byte identical to
/// `serde_json::to_vec(&Value::Object(document))` whenever
/// `projection_bytes == serde_json::to_vec(&projection)` (pinned by
/// `raw_document_splice_matches_canonical_serialization_byte_for_byte`).
fn serialize_document_with_raw_projection(
    document: &Map<String, Value>,
    projection_bytes: &[u8],
) -> io::Result<Vec<u8>> {
    let mut output = Vec::with_capacity(document.len() * 24 + projection_bytes.len() + 2);
    output.push(b'{');
    for (index, (key, value)) in document.iter().enumerate() {
        if index > 0 {
            output.push(b',');
        }
        serde_json::to_writer(&mut output, key)
            .map_err(|_| invalid_data("document key cannot be encoded"))?;
        output.push(b':');
        if key == "projection" {
            output.extend_from_slice(projection_bytes);
        } else {
            serde_json::to_writer(&mut output, value)
                .map_err(|_| invalid_data("document value cannot be encoded"))?;
        }
    }
    output.push(b'}');
    Ok(output)
}

#[derive(Serialize)]
struct TurnUpsertSyncEvent<'a> {
    #[serde(rename = "attemptId", skip_serializing_if = "Option::is_none")]
    attempt_id: Option<&'a str>,
    contract: &'static str,
    #[serde(rename = "conversationId")]
    conversation_id: &'a str,
    #[serde(rename = "occurredAt")]
    occurred_at_ms: u64,
    payload: TurnUpsertPayload<'a>,
    #[serde(rename = "syncSeq")]
    sync_sequence: u64,
    #[serde(rename = "turnId", skip_serializing_if = "Option::is_none")]
    turn_id: Option<&'a str>,
    #[serde(rename = "type")]
    change_type: &'static str,
}

fn store_turn_upsert_retention_indexes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    sync_sequence: u64,
    occurred_at_ms: u64,
) -> io::Result<()> {
    // Byte-identical to `sync_age_index_value` for non-attempt events: the
    // locator is the three scalar fields under serde_json's sorted map,
    // written directly to skip the intermediate map allocation.
    let mut value = Vec::with_capacity(48 + conversation_id.len());
    value.extend_from_slice(b"{\"conversationId\":");
    serde_json::to_writer(&mut value, conversation_id)
        .map_err(|_| invalid_data("conversation sync retention index cannot be encoded"))?;
    value.extend_from_slice(b",\"occurredAt\":");
    value.extend_from_slice(occurred_at_ms.to_string().as_bytes());
    value.extend_from_slice(b",\"syncSeq\":");
    value.extend_from_slice(sync_sequence.to_string().as_bytes());
    value.push(b'}');
    database.entity_put(
        transaction,
        conversation_sync_age_index_key(
            transaction,
            occurred_at_ms,
            conversation_id,
            sync_sequence,
        )?,
        value,
    )?;
    transaction.note_conversation_commit(conversation_id, sync_sequence)
}

/// Claim the next conversation sync sequence. The head row is the single
/// allocator: it advances in the same transaction that stores the event, so
/// `(conversation, sequence)` uniqueness holds across live writes and the
/// offline importer (which restores head and events consistently). The
/// caller computes the conversation prefix once and shares it with the
/// event-key construction.
fn next_sync_sequence(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    prefix: &[u8],
) -> io::Result<(EntityKey, u64)> {
    let head_key = entity_key(transaction, CONVERSATION_SYNC_HEAD_NAMESPACE, prefix)?;
    let sequence = decode_u64(
        database.entity_get(transaction, &head_key)?,
        "conversation sync head is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("conversation sync sequence overflow"))?;
    Ok((head_key, sequence))
}

/// Persist one already-encoded sync event and its retention footprint, then
/// advance the head. The event bytes are caller-serialized and the sequence
/// is head-allocated, so the document insert stores the byte-identical
/// versioned-document envelope without re-validating or re-reading the key.
#[allow(clippy::too_many_arguments)]
fn store_sync_event_bytes(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    prefix: &[u8],
    head_key: EntityKey,
    sequence: u64,
    event_bytes: Vec<u8>,
    occurred_at_ms: u64,
    committed_at_ms: u64,
) -> io::Result<()> {
    // `{conversation}\u{1f}{sequence:020}` written without the fmt
    // machinery: the logical key is rebuilt per append on the hot path.
    let mut logical_key = String::with_capacity(conversation_id.len() + 21);
    logical_key.push_str(conversation_id);
    logical_key.push('\u{1f}');
    let mut digits = [b'0'; 20];
    let mut remainder = sequence;
    for digit in digits.iter_mut().rev() {
        *digit = b'0' + (remainder % 10) as u8;
        remainder /= 10;
    }
    // sequence is u64, so at most 20 digits and the buffer never overflows.
    logical_key.push_str(std::str::from_utf8(&digits).expect("ASCII digits"));
    let mut encoded_key = prefix.to_vec();
    encoded_key.extend_from_slice(&sequence.to_be_bytes());
    crate::versioned_document::put_fresh_prevalidated(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: entity_key(transaction, CONVERSATION_SYNC_EVENT_NAMESPACE, &encoded_key)?,
            namespace: SYNC_DOCUMENT_IDENTITY.to_owned(),
            logical_key,
            value_json: event_bytes,
            // Freshness comes from the head-allocated sequence, not from a
            // version check (see `put_fresh_prevalidated`).
            expected_version: None,
            updated_at_ms: committed_at_ms,
        },
    )?;
    store_turn_upsert_retention_indexes(
        database,
        transaction,
        conversation_id,
        sequence,
        occurred_at_ms,
    )?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())?;
    Ok(())
}

/// Byte-exact `TurnUpsertSyncEvent` encoding for the settled-append shape —
/// exactly one turn, zero or one attempt, no queue mutations — splicing the
/// already-encoded public turn bytes in place. Assembling directly avoids
/// re-validating the turn through `RawValue` and avoids the intermediate
/// event structure; the output is pinned byte-for-byte against the generic
/// derive encoding (which remains the path for every other `turn.upsert`
/// shape) by `settled_sync_event_splice_matches_the_derive_encoding`.
#[allow(clippy::too_many_arguments)]
fn settled_turn_upsert_event_bytes(
    conversation_id: &str,
    turn_id: Option<&str>,
    attempt_id: Option<&str>,
    revision: u64,
    occurred_at_ms: u64,
    sync_sequence: u64,
    turn_bytes: &[u8],
    attempt: Option<&Value>,
) -> io::Result<Vec<u8>> {
    let mut output = Vec::with_capacity(turn_bytes.len() + 256);
    output.push(b'{');
    if let Some(attempt_id) = attempt_id.filter(|identity| !identity.is_empty()) {
        output.extend_from_slice(b"\"attemptId\":");
        serde_json::to_writer(&mut output, attempt_id)
            .map_err(|_| invalid_data("sync event cannot be encoded"))?;
        output.push(b',');
    }
    output
        .extend_from_slice(b"\"contract\":\"tofu.conversation-sync.event/v1\",\"conversationId\":");
    serde_json::to_writer(&mut output, conversation_id)
        .map_err(|_| invalid_data("sync event cannot be encoded"))?;
    output.extend_from_slice(b",\"occurredAt\":");
    output.extend_from_slice(occurred_at_ms.to_string().as_bytes());
    output.extend_from_slice(b",\"payload\":{\"attempts\":[");
    if let Some(attempt) = attempt {
        serde_json::to_writer(&mut output, attempt)
            .map_err(|_| invalid_data("sync event cannot be encoded"))?;
    }
    output.extend_from_slice(b"],\"conversationRevision\":");
    output.extend_from_slice(revision.to_string().as_bytes());
    output.extend_from_slice(b",\"turns\":[");
    output.extend_from_slice(turn_bytes);
    output.extend_from_slice(b"]},\"syncSeq\":");
    output.extend_from_slice(sync_sequence.to_string().as_bytes());
    if let Some(turn_id) = turn_id.filter(|identity| !identity.is_empty()) {
        output.extend_from_slice(b",\"turnId\":");
        serde_json::to_writer(&mut output, turn_id)
            .map_err(|_| invalid_data("sync event cannot be encoded"))?;
    }
    output.extend_from_slice(b",\"type\":\"turn.upsert\"}");
    Ok(output)
}

/// Byte-exact `AppendSettledResponse` encoding splicing the same public turn
/// bytes the sync event carries (pinned next to the event splice).
fn append_settled_response_bytes(
    attempt: &Option<Value>,
    revision: u64,
    turn_bytes: &[u8],
) -> io::Result<Vec<u8>> {
    let mut output = Vec::with_capacity(turn_bytes.len() + 96);
    output.extend_from_slice(b"{\"attempt\":");
    match attempt {
        Some(attempt) => serde_json::to_writer(&mut output, attempt)
            .map_err(|_| invalid_data("response cannot be encoded"))?,
        None => output.extend_from_slice(b"null"),
    }
    output.extend_from_slice(b",\"conversationRevision\":");
    output.extend_from_slice(revision.to_string().as_bytes());
    output.extend_from_slice(b",\"turn\":");
    output.extend_from_slice(turn_bytes);
    output.push(b'}');
    Ok(output)
}

fn store_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AppendSettledRequest,
    turn_bytes: &[u8],
    attempt: Option<&Value>,
    revision: u64,
) -> io::Result<()> {
    let prefix = conversation_prefix(&request.conversation_id)?;
    let (head_key, sequence) = next_sync_sequence(database, transaction, &prefix)?;
    let event_bytes = settled_turn_upsert_event_bytes(
        &request.conversation_id,
        Some(&request.turn_id),
        request.attempt_id.as_deref(),
        revision,
        request.created_at_ms,
        sequence,
        turn_bytes,
        attempt,
    )?;
    store_sync_event_bytes(
        database,
        transaction,
        &request.conversation_id,
        &prefix,
        head_key,
        sequence,
        event_bytes,
        request.created_at_ms,
        request.committed_at_ms,
    )
}

#[allow(clippy::too_many_arguments)]
fn store_turn_upsert_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turns: Vec<&RawValue>,
    attempts: Vec<Value>,
    queue_item_upserts: Vec<Value>,
    removed_queue_ids: Vec<String>,
    revision: u64,
    occurred_at_ms: u64,
    committed_at_ms: u64,
    turn_id: Option<&str>,
    attempt_id: Option<&str>,
) -> io::Result<()> {
    let prefix = conversation_prefix(conversation_id)?;
    let (head_key, sequence) = next_sync_sequence(database, transaction, &prefix)?;
    let event = TurnUpsertSyncEvent {
        attempt_id: attempt_id.filter(|identity| !identity.is_empty()),
        contract: "tofu.conversation-sync.event/v1",
        conversation_id,
        occurred_at_ms,
        payload: TurnUpsertPayload {
            attempts: attempts.iter().collect(),
            conversation_revision: revision,
            queue_item_upserts,
            removed_queue_ids,
            turns,
        },
        sync_sequence: sequence,
        turn_id: turn_id.filter(|identity| !identity.is_empty()),
        change_type: "turn.upsert",
    };
    let event_bytes =
        serde_json::to_vec(&event).map_err(|_| invalid_data("sync event cannot be encoded"))?;
    store_sync_event_bytes(
        database,
        transaction,
        conversation_id,
        &prefix,
        head_key,
        sequence,
        event_bytes,
        occurred_at_ms,
        committed_at_ms,
    )
}

/// `queue.injection.ack` publishes one `turn.upsert` change whose payload
/// carries only the drained inbox identities — matching the legacy capture,
/// it deliberately carries no `conversationRevision` because the command
/// never advances the conversation header revision.
pub(crate) fn store_queue_injection_ack_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    removed_queue_ids: &[String],
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
    let payload = json!({
        "turns": [],
        "attempts": [],
        "removedQueueIds": serde_json::to_value(removed_queue_ids)
            .map_err(|_| invalid_data("removed queue identities cannot be encoded"))?,
    });
    let event = conversation_sync_event(
        conversation_id,
        sequence,
        "turn.upsert",
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
                .map_err(|_| invalid_data("sync event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: occurred_at_ms.max(1),
        },
    )?;
    store_sync_retention_indexes(database, transaction, &event)?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())?;
    Ok(())
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
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())?;
    Ok(())
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

pub(crate) struct AttemptEventAppend<'a> {
    pub conversation_id: &'a str,
    pub turn_id: &'a str,
    pub attempt_id: &'a str,
    pub projection_revision: u64,
    pub event_type: &'a str,
    pub payload: Value,
    pub occurred_at_ms: u64,
    pub publish_conversation_sync: bool,
}

pub(crate) fn append_attempt_event(
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
    let event_json = serde_json::to_vec(&event)
        .map_err(|_| invalid_data("attempt event cannot be encoded"))?;
    let event_json_bytes = event_json.len();
    crate::versioned_document::put(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: attempt_event_key(transaction, attempt_id, sequence)?,
            namespace: ATTEMPT_EVENT_DOCUMENT_IDENTITY.to_owned(),
            logical_key: format!("{attempt_id}\u{1f}{sequence:020}"),
            value_json: event_json,
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
            json!({"event": {"seq": sequence}}),
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
                namespace: sync_attempt_reference::DOCUMENT_IDENTITY.to_owned(),
                logical_key,
                value_json: sync_attempt_reference::encode(&envelope, event_json_bytes)?,
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
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())?;
    Ok(())
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
    // The destination's message stream is rewritten by the clone: archive
    // reads there fall back to the exact lane read until appends rebuild.
    crate::transcript_archive::invalidate(database, transaction, destination_conversation_id)?;
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
            .filter(|bytes| *bytes <= MAX_LEGACY_DOCUMENT_BYTES)
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
            .filter(|bytes| *bytes <= MAX_LEGACY_DOCUMENT_BYTES)
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
        crate::versioned_document::put_with_blob_owner_bounded(
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
            transaction.owner_user_id(),
            crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
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
            lane_state_key(transaction, destination_conversation_id, lane_id)?,
            encode_lane_state(&LaneState {
                head: lane_heads[lane_id],
                count: Some(*count),
                // The clone path does not track the destination tail: the
                // next append's one legacy rescan re-seeds it.
                last_turn_id: None,
            }),
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

/// Immutable bytes minted only by this module's settled-append producer.
/// The outer response has exactly attempt/conversationRevision/turn, no `ok`
/// or root Serde marker. Source document staging validates the raw projection
/// before it is spliced here. This proves receipt eligibility only; semantic
/// response integrity/depth validation remains mandatory.
pub(crate) struct SettledAppendResponse { bytes: Vec<u8> }
impl SettledAppendResponse {
    pub(crate) fn into_bytes(self) -> Vec<u8> { self.bytes }
}

pub(crate) fn append_settled(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AppendSettledRequest,
) -> io::Result<Vec<u8>> {
    append_settled_with_receipt_proof(database, transaction, request)
        .map(SettledAppendResponse::into_bytes)
}

pub(crate) fn append_settled_with_receipt_proof(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &AppendSettledRequest,
) -> io::Result<SettledAppendResponse> {
    let _semantic_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_SEMANTIC);
    let _ta_claims = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_CLAIMS);
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
    // Fused ensure + epoch + advance: one header load/store per append on the
    // hot path (see conversation_header::advance_for_turn_append). The
    // revision is consumed by the sync event and response below; the
    // transaction aborts atomically if any later step fails, so performing
    // the advance here is equivalent to the legacy end-of-function call.
    drop(_ta_claims);
    let _ta_header = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_HEADER);
    let (execution_epoch, revision) = crate::conversation_header::advance_for_turn_append(
        database,
        transaction,
        &request.conversation_id,
        &request.defaults,
        request.created_at_ms,
        request.committed_at_ms,
        request.lane_id == "main",
    )?;
    drop(_ta_header);
    let _ta_lane = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_LANE_READS);
    let head_key = entity_key(
        transaction,
        TURN_LANE_HEAD_NAMESPACE,
        &lane_prefix(&request.conversation_id, &request.lane_id)?,
    )?;
    // Fused lane bookkeeping (2026-09-20): one witness-verified read carries
    // head ordinal, exact lane count, and the predecessor identity; the
    // legacy head/count/scan triple-descent survives only for legacy rows.
    let head_key = lane_state_key(transaction, &request.conversation_id, &request.lane_id)?;
    let lane_state = read_lane_state(
        database,
        transaction,
        &request.conversation_id,
        &request.lane_id,
    )?;
    let ordinal = lane_state
        .as_ref()
        .map(|state| {
            state
                .head
                .checked_add(1)
                .ok_or_else(|| invalid_data("turn lane ordinal overflow"))
        })
        .transpose()?
        .unwrap_or(0);
    let lane_count = lane_count_of(
        database,
        transaction,
        &request.conversation_id,
        &request.lane_id,
        lane_state.as_ref(),
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("turn lane count overflow"))?;
    let parent_turn_id = match lane_last_turn_id(
        database,
        transaction,
        &request.conversation_id,
        &request.lane_id,
        lane_state.as_ref(),
    )? {
        None => Value::Null,
        Some(turn_id) => Value::String(turn_id),
    };
    let next_lane_state = LaneState {
        head: ordinal,
        count: Some(lane_count),
        last_turn_id: Some(request.turn_id.clone()),
    };
    drop(_ta_lane);
    let _ta_projection =
        crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_PROJECTION);
    let settlement = match serde_json::from_slice::<Value>(&request.settlement_json) {
        Ok(Value::Object(map)) => map,
        _ => return Err(invalid_input("turn settlement must be an object")),
    };
    let activity_timestamp =
        effective_activity_timestamp(request.projection_timestamp.as_ref(), request.created_at_ms);
    // The compiler's canonical RawValue escape IS the stored projection
    // form: its bytes splice into the document, the sync event and the
    // response verbatim — no second typed parse/escape of the content on
    // this path. For compact producers this is byte-identical to the former
    // typed re-escape; non-canonical spellings stay self-consistent under
    // replay, exactly like the receipt canonical layer.
    let projection_bytes = &request.projection_json;
    let projection_json_len = projection_bytes.len();
    drop(_ta_projection);
    let _ta_document = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_DOCUMENT);
    let mut document = json!({
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
        "projectionRevision": 1,
        "createdAt": request.created_at_ms,
        "updatedAt": request.created_at_ms,
        "_executionEpoch": execution_epoch
    });
    // The in-memory placeholder never reaches the stored bytes: both
    // serialization points splice `projection_bytes` over the entry, and
    // the terminalize path (restored-document epoch mismatch) parses the
    // projection lazily on demand instead.
    document
        .as_object_mut()
        .expect("turn document literal is an object")
        .insert("projection".to_owned(), Value::Object(Map::new()));
    document
        .as_object_mut()
        .expect("turn document literal is an object")
        .insert("settlement".to_owned(), Value::Object(settlement));
    // The stored document serializes canonically except that the projection
    // field reuses the exact bytes escaped above, never a second escape.
    let document_json = serialize_document_with_raw_projection(
        document
            .as_object()
            .ok_or_else(|| invalid_data("turn document is not an object"))?,
        &projection_bytes,
    )?;
    let document_key = turn_key(transaction, &request.conversation_id, &request.turn_id)?;
    let (_, stored) = crate::versioned_document::put_returning_envelope(
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
    drop(_ta_document);
    let _ta_indexes = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_INDEXES);
    database.entity_put(transaction, turn_claim_key, claim_owner.clone())?;
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
    database.entity_put(transaction, head_key, encode_lane_state(&next_lane_state))?;
    drop(_ta_indexes);
    let _ta_dirty = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_DIRTY);
    crate::search_dirty::mark(
        database,
        transaction,
        TURN_SEARCH_DIRTY_NAMESPACE,
        &request.conversation_id,
    )?;
    drop(_ta_dirty);
    // Move the document map into the public projection instead of a
    // serialize + re-parse round trip.
    let _ta_sync = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_SYNC);
    let document_map = match document {
        Value::Object(map) => map,
        _ => return Err(invalid_data("turn document is not an object")),
    };
    let turn = public_turn(document_map, execution_epoch)?;
    let attempt = request.attempt_id.as_ref().map(|attempt_id| {
        let mut document = json!({
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
        });
        if request.status == "superseded" {
            document
                .as_object_mut()
                .expect("attempt literal is an object")
                .insert(
                    "supersededAt".to_owned(),
                    Value::from(request.created_at_ms),
                );
        }
        document
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
        if request.status == "superseded" {
            database.entity_put(
                transaction,
                attempt_superseded_index_key(
                    transaction,
                    request.created_at_ms,
                    &request.conversation_id,
                    attempt_id,
                )?,
                Vec::new(),
            )?;
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
                recovery_index_value(attempt_document, projection_json_len)?,
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
    // Serialize the public turn exactly once, splicing the same projection
    // bytes: the sync event and the response embed those bytes verbatim
    // instead of re-escaping or re-validating the content (byte-identical,
    // pinned by the raw-encoding tests).
    // Maintain the compact transcript archive in the same transaction:
    // hydration then reads a handful of page documents instead of
    // materializing every turn document. Only this append path maintains
    // the archive; every other lane mutation invalidates it.
    let turn_map = match turn {
        Value::Object(map) => map,
        _ => return Err(invalid_data("public turn is not an object")),
    };
    let turn_bytes = serialize_document_with_raw_projection(&turn_map, &projection_bytes)?;
    store_sync_event(
        database,
        transaction,
        request,
        &turn_bytes,
        attempt.as_ref(),
        revision,
    )?;
    append_settled_response_bytes(&attempt, revision, &turn_bytes)
        .map(|bytes| SettledAppendResponse { bytes })
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
    let (_, stored) = crate::versioned_document::put_returning_envelope(
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
    let count_key = attempt_turn_count_key(transaction, conversation_id, turn_id)?;
    let next_count = decode_u64(
        database.entity_get(transaction, &count_key)?,
        "attempt Turn count is malformed",
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("attempt Turn count overflow"))?;
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
    database.entity_put(transaction, count_key, next_count.to_le_bytes().to_vec())?;
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
    if let Some(expected) = request.expected_parent_projection_revision {
        let parent_id = request.parent_turn_id.as_ref()
            .ok_or_else(|| typed_conflict(TurnConflictKind::ParentInvalid))?;
        let encoded = get(database, transaction, &request.conversation_id, parent_id)?
            .ok_or_else(|| typed_conflict(TurnConflictKind::ParentInvalid))?;
        let parent: Value = serde_json::from_slice(&encoded)
            .map_err(|error| invalid_data(&format!("invalid recovery parent: {error}")))?;
        if parent.get("projectionRevision").and_then(Value::as_u64) != Some(expected) {
            return Err(typed_conflict(TurnConflictKind::LaneAdvanced));
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
    let lane_state = read_lane_state(
        database,
        transaction,
        &request.conversation_id,
        &request.lane_id,
    )?;
    let previous_ordinal = lane_state.as_ref().map(|state| state.head);
    let input_ordinal = previous_ordinal
        .map(|value| {
            value
                .checked_add(1)
                .ok_or_else(|| invalid_data("turn lane ordinal overflow"))
        })
        .transpose()?
        .unwrap_or(0);
    let previous_tail = lane_last_turn_id(
        database,
        transaction,
        &request.conversation_id,
        &request.lane_id,
        lane_state.as_ref(),
    )?;
    if (request.require_parent_is_lane_tail || request.require_parent_not_stopped)
        && previous_tail.as_ref() != request.parent_turn_id.as_ref()
    {
        return Err(typed_conflict(TurnConflictKind::LaneAdvanced));
    }
    if request.require_parent_not_stopped {
        let parent_id = previous_tail.as_ref()
            .ok_or_else(|| typed_conflict(TurnConflictKind::LaneAdvanced))?;
        let encoded = get(database, transaction, &request.conversation_id, parent_id)?
            .ok_or_else(|| typed_conflict(TurnConflictKind::LaneAdvanced))?;
        let parent: Value = serde_json::from_slice(&encoded)
            .map_err(|error| invalid_data(&format!("invalid continuation parent: {error}")))?;
        let status = parent.get("status").and_then(Value::as_str).unwrap_or("");
        let restarted = status == "interrupted"
            && parent.get("settlement").and_then(|value| value.get("cause"))
                .and_then(Value::as_str) == Some("server_restart");
        if !matches!(status, "completed" | "failed") && !restarted {
            return Err(typed_conflict(TurnConflictKind::LaneAdvanced));
        }
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
    let lane_count = lane_count_of(
        database,
        transaction,
        &request.conversation_id,
        &request.lane_id,
        lane_state.as_ref(),
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
            // Byte counting only: a serialization failure must not panic the
            // commit path; a failed count records zero for the metric.
            serde_json::to_vec(&request.input_projection).map_or(0, |bytes| bytes.len()),
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
        // Byte counting only; see the input-projection site above.
        serde_json::to_vec(&output_projection).map_or(0, |bytes| bytes.len()),
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
    database.entity_put(
        transaction,
        head_key,
        encode_lane_state(&LaneState {
            head: output_ordinal,
            count: Some(lane_count),
            last_turn_id: Some(request.output_turn_id.clone()),
        }),
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
    // Serialize each public turn exactly once; the sync event and the
    // response embed the same bytes (see AppendSettledResponse note above).
    let submitted_raw = RawValue::from_string(
        serde_json::to_string(&submitted)
            .map_err(|_| invalid_data("submitted Turn cannot be encoded"))?,
    )
    .map_err(|_| invalid_data("submitted Turn encoding is not valid JSON"))?;
    let output_raw = RawValue::from_string(
        serde_json::to_string(&output)
            .map_err(|_| invalid_data("output Turn cannot be encoded"))?,
    )
    .map_err(|_| invalid_data("output Turn encoding is not valid JSON"))?;
    store_turn_upsert_sync_event(
        database,
        transaction,
        &request.conversation_id,
        vec![&submitted_raw, &output_raw],
        vec![public_output_attempt.clone()],
        queue_item.clone().into_iter().collect(),
        Vec::new(),
        revision,
        request.created_at_ms,
        request.committed_at_ms,
        Some(&request.output_turn_id),
        Some(&request.output_attempt_id),
    )?;
    let queued = queue_item.is_some();
    let response = CreatePairResponse {
        needs_start: queue_id.is_empty(),
        attempt: &public_output_attempt,
        conversation_revision: revision,
        idempotent_replay: false,
        position: queue_item
            .as_ref()
            .map(|item| item.get("position").cloned().unwrap_or(Value::Null)),
        queue_id: queue_item.as_ref().map(|_| queue_id.to_owned()),
        queue_item,
        queued,
        stream_cursor: 1,
        submitted_turn: &submitted_raw,
        turn: &output_raw,
    };
    encode_response_raw(&response)
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
    let submitted_raw = RawValue::from_string(
        serde_json::to_string(&submitted)
            .map_err(|_| invalid_data("submitted Turn cannot be encoded"))?,
    )
    .map_err(|_| invalid_data("submitted Turn encoding is not valid JSON"))?;
    let output_raw = RawValue::from_string(
        serde_json::to_string(&output)
            .map_err(|_| invalid_data("output Turn cannot be encoded"))?,
    )
    .map_err(|_| invalid_data("output Turn encoding is not valid JSON"))?;
    store_turn_upsert_sync_event(
        database,
        transaction,
        &request.conversation_id,
        vec![&submitted_raw, &output_raw],
        vec![public_attempt.clone()],
        Vec::new(),
        vec![request.queue_id.clone()],
        revision,
        request.committed_at_ms,
        request.committed_at_ms,
        Some(&binding.output_turn_id),
        Some(&binding.attempt_id),
    )?;
    encode_response_raw(&QueueResumeResponse {
        needs_start: true,
        attempt: &public_attempt,
        conversation_revision: revision,
        idempotent_replay: false,
        queue_id: &request.queue_id,
        queued: false,
        stream_cursor: 1,
        submitted_turn: &submitted_raw,
        turn: &output_raw,
    })
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
    if crate::queue::cancel_background_receipt(
        database,
        transaction,
        &request.conversation_id,
        &request.queue_id,
        request.committed_at_ms,
    )? {
        let revision = crate::conversation_header::advance_for_turn(
            database,
            transaction,
            &request.conversation_id,
            request.committed_at_ms,
            request.committed_at_ms,
            false,
        )?;
        store_delete_sync_event(
            database,
            transaction,
            &request.conversation_id,
            &[],
            std::slice::from_ref(&request.queue_id),
            revision,
            request.committed_at_ms,
        )?;
        return encode_response(&json!({
            "conversationId": request.conversation_id,
            "conversationRevision": revision,
            "queueId": request.queue_id,
            "cancelled": true,
            "inputTurn": Value::Null,
            "deletedTurnIds": []
        }));
    }
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
    update_projection_mode(database, transaction, request, ProjectionUpdateMode::TypedSettledEdit)
}

fn update_projection_mode(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &ProjectionUpdateRequest,
    mode: ProjectionUpdateMode,
) -> io::Result<Vec<u8>> {
    let staged = stage_projection_update(database, transaction, request, mode)?;
    let activity_at_ms = if request.preserve_activity {
        crate::conversation_header::activity_timestamp(
            database,
            transaction,
            &request.conversation_id,
        )?
    } else {
        request.updated_at_ms
    };
    let conversation_revision = crate::conversation_header::advance_for_turn(
        database,
        transaction,
        &request.conversation_id,
        activity_at_ms,
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

/// Scoped command acceptance uses the owned Turn transaction and its sync publication.
/// A process-local waiter is only a notification consumer of this receipt.
pub(crate) fn answer_guidance(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
    answer_json: &[u8],
    now_ms: u64,
) -> io::Result<Vec<u8>> {
    let answer: Value = serde_json::from_slice(answer_json)
        .map_err(|_| invalid_input("Invalid human guidance answer"))?;
    let text = |key: &str, limit: usize| -> io::Result<&str> {
        answer.get(key).and_then(Value::as_str)
            .filter(|s| !s.trim().is_empty() && s.chars().count() <= limit)
            .ok_or_else(|| invalid_input("Invalid human guidance answer"))
    };
    let guidance_id = text("guidanceId", crate::generated_tofudb_ir::MAX_HUMAN_GUIDANCE_IDENTITY_CHARACTERS)?;
    let response = text("response", crate::generated_tofudb_ir::MAX_HUMAN_GUIDANCE_RESPONSE_CHARACTERS)?;
    let command_id = text("commandId", crate::generated_tofudb_ir::MAX_HUMAN_GUIDANCE_IDENTITY_CHARACTERS)?;
    let encoded_turn = get(database, transaction, conversation_id, turn_id)?
        .ok_or_else(|| not_found("Human guidance not ready"))?;
    let turn: Value = serde_json::from_slice(&encoded_turn)
        .map_err(|_| invalid_data("Malformed guidance Turn"))?;
    let mut projection = turn.get("projection").and_then(Value::as_object).cloned()
        .ok_or_else(|| invalid_data("Malformed guidance projection"))?;
    let rounds = projection.get_mut("toolRounds").and_then(Value::as_array_mut)
        .ok_or_else(|| not_found("Human guidance not ready"))?;
    let matches: Vec<usize> = rounds.iter().enumerate()
        .filter(|(_, r)| r.get("guidanceId").and_then(Value::as_str) == Some(guidance_id))
        .map(|(index, _)| index).collect();
    if matches.len() != 1 { return Err(not_found("Human guidance not ready")); }
    if rounds.iter().any(|r| r.get("guidanceAnswerCommandId").and_then(Value::as_str) == Some(command_id)
        && (r.get("guidanceId").and_then(Value::as_str) != Some(guidance_id)
            || r.get("guidanceResponse").and_then(Value::as_str) != Some(response))) {
        return Err(conflict("Human guidance command already accepted differently"));
    }
    let question = rounds[matches[0]].as_object_mut()
        .ok_or_else(|| not_found("Human guidance not ready"))?;
    if question.get("toolName").and_then(Value::as_str) != Some("ask_human") {
        return Err(not_found("Human guidance not ready"));
    }
    if question.get("guidanceAnswerCommandId").is_some() {
        if question.get("guidanceResponse").and_then(Value::as_str) != Some(response) {
            return Err(conflict("Human guidance already answered differently"));
        }
        let revision = crate::conversation_header::revision(database, transaction, conversation_id)?;
        return encode_response(&json!({"turn": turn, "conversationRevision": revision,
                                      "idempotentReplay": true}));
    }
    if !matches!(turn.get("status").and_then(Value::as_str), Some("pending" | "running"))
        || !matches!(question.get("status").and_then(Value::as_str), Some("pending" | "awaiting_human")) {
        return Err(not_found("Human guidance not ready"));
    }
    if question.get("guidanceType").and_then(Value::as_str) == Some("choice") {
        let options = question.get("guidanceOptions").and_then(Value::as_array)
            .filter(|v| !v.is_empty()).ok_or_else(|| invalid_input("No valid declared choices"))?;
        let mut valid_response = false;
        for option in options {
            let label = option.as_str().or_else(|| option.get("label").and_then(Value::as_str))
                .filter(|v| !v.is_empty()).ok_or_else(|| invalid_input("No valid declared choices"))?;
            valid_response |= response == label || response.starts_with(&format!("{label}\nuser_note: "));
        }
        if !valid_response { return Err(invalid_input("Response is not a declared choice")); }
    }
    complete_guidance_round(question, response, command_id);
    let request = ProjectionUpdateRequest {
        preserve_activity: false, conversation_id: conversation_id.to_owned(), turn_id: turn_id.to_owned(),
        projection_json: serde_json::to_vec(&projection).map_err(|_| invalid_data("Guidance projection encoding failed"))?,
        expected_projection_revision: turn["projectionRevision"].as_u64()
            .ok_or_else(|| invalid_data("Malformed guidance revision"))?,
        updated_at_ms: now_ms, committed_at_ms: now_ms,
    };
    let result = update_projection_mode(database, transaction, &request, ProjectionUpdateMode::FencedLiveSteer)?;
    if let Some(attempt_id) = turn.get("currentAttemptId").and_then(Value::as_str) {
        if let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? {
            if attempt_is_live(&loaded.document) {
                let created_at = loaded.document.get("createdAt").and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("Malformed guidance recovery timestamp"))?;
                let reply: Value = serde_json::from_slice(&result)
                    .map_err(|_| invalid_data("Malformed guidance reply"))?;
                let projection_bytes = serde_json::to_vec(&reply["turn"]["projection"])
                    .map_err(|_| invalid_data("Guidance projection encoding failed"))?.len();
                database.entity_put(transaction, recovery_index_key(transaction, created_at, attempt_id)?,
                    recovery_index_value(&loaded.document, projection_bytes)?)?;
            }
        }
    }
    Ok(result)
}

fn complete_guidance_round(question: &mut Map<String, Value>, response: &str, command_id: &str) {
    let guidance_id = question.get("guidanceId").cloned();
    question.insert("guidanceResponse".to_owned(), json!(response));
    question.insert("guidanceAnswerCommandId".to_owned(), json!(command_id));
    question.insert("status".to_owned(), json!("completed"));
    question.insert("resultSummary".to_owned(), json!(response));
    question.insert("resultText".to_owned(), json!(format!("Human response: {response}")));
    question.insert("toolContent".to_owned(), json!(format!("Human response: {response}")));
    for key in ["results", "toolMeta"] {
        if let Some(entries) = question.get_mut(key).and_then(Value::as_array_mut) {
            for entry in entries {
                if let Some(entry) = entry.as_object_mut() {
                    if entry.get("guidanceId") == guidance_id.as_ref() {
                        entry.insert("userResponse".to_owned(), json!(response));
                        entry.insert("waiting".to_owned(), json!(false));
                        entry.insert("received".to_owned(), json!(true));
                    }
                }
            }
        }
    }
}

fn preserve_guidance_answers(previous: &Map<String, Value>, candidate: &mut Map<String, Value>) -> bool {
    let accepted: Vec<&Value> = previous.get("toolRounds").and_then(Value::as_array)
        .map(|rounds| rounds.iter().filter(|r| r.get("guidanceAnswerCommandId").is_some()).collect())
        .unwrap_or_default();
    if accepted.is_empty() { return false; }
    let rounds = candidate.entry("toolRounds".to_owned()).or_insert_with(|| json!([]));
    if !rounds.is_array() { *rounds = json!([]); }
    let Some(rounds) = rounds.as_array_mut() else { return false; };
    let mut changed = false;
    for receipt in accepted {
        if let Some(question) = rounds.iter_mut().find(|r| r.get("guidanceId") == receipt.get("guidanceId")) {
            let before = question.clone();
            if let (Some(question), Some(response), Some(command_id)) = (
                question.as_object_mut(), receipt.get("guidanceResponse").and_then(Value::as_str),
                receipt.get("guidanceAnswerCommandId").and_then(Value::as_str)) {
                complete_guidance_round(question, response, command_id);
            }
            changed |= before != *question;
        } else {
            rounds.push(receipt.clone()); changed = true;
        }
    }
    changed
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
        preserve_activity: false,
        conversation_id: request.conversation_id.clone(),
        turn_id,
        projection_json: serde_json::to_vec(&next_projection)
            .map_err(|_| invalid_data("turn projection cannot be encoded"))?,
        expected_projection_revision: base_revision,
        updated_at_ms: request.updated_at_ms,
        committed_at_ms: request.committed_at_ms,
    };
    stage_projection_update(
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
    // Both the public Turn and the injection-array patch grow with history;
    // peers hydrate the post-commit snapshot from one tiny invalidation.
    store_activity_sync_event(
        database,
        transaction,
        &request.conversation_id,
        &projection_request.turn_id,
        conversation_revision,
        request.committed_at_ms,
    )?;
    encode_response(&json!({
        "steered": true,
        "injectionId": request.command_id,
        "blockId": block_id,
        "conversationId": request.conversation_id,
        "turnId": projection_request.turn_id,
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

pub(crate) fn visible_tool_segment(round: &Map<String, Value>, position: usize) -> Value {
    // Match the public segment constructor in tasks_pkg/segments/_assemble.py.
    // Missing/non-string verdicts are omitted, never serialized as status:null.
    let mut result = Map::from_iter([(
        "content".to_owned(),
        round.get("toolContent").cloned().unwrap_or(Value::Null),
    )]);
    if let Some(Value::String(status)) = round.get("status") {
        if !status.is_empty() {
            result.insert("status".to_owned(), json!(status));
        }
    }
    if let Some(model_content) = round.get("toolModelContent").filter(|value| !value.is_null()) {
        result.insert("modelContent".to_owned(), model_content.clone());
    }
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
        ("result".to_owned(), Value::Object(result)),
    ]);
    // Public JavaScript round indices must be exact, nonnegative integers.
    const MAX_PUBLIC_ROUND_NUMBER: u64 = 9_007_199_254_740_991;
    if let Some(round_number) = round.get("roundNum").and_then(Value::as_u64)
        .filter(|number| *number <= MAX_PUBLIC_ROUND_NUMBER)
    {
        segment.insert("roundNum".to_owned(), json!(round_number));
    }
    if !attempt_id.is_empty() {
        segment.insert("attemptId".to_owned(), json!(attempt_id));
    }
    if !task_id.is_empty() {
        segment.insert("taskId".to_owned(), json!(task_id));
    }
    Value::Object(segment)
}

fn visible_stable_segments(projection: &mut Map<String, Value>, actor: &str) {
    let preserved_notes = crate::turn_projection_segments::preserved_notes(projection.get("segments"));
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
    // Completion-only imports may already carry accepted context notes.
    // Preserve them with the same round/attempt ordering as the producer;
    // dropping them here makes an identical terminal replay add new evidence.
    for note in preserved_notes {
        let round = note.get("llmRound").and_then(Value::as_i64);
        let scope = note.get("attemptId").and_then(Value::as_str).unwrap_or("");
        let position = segments.iter().position(|segment| segment["terminal"] == true || (
            round.zip(segment["llmRound"].as_i64()).is_some_and(|(a, b)| b > a)
                && (scope.is_empty() || segment["attemptId"].as_str() == Some(scope))))
            .unwrap_or(segments.len());
        segments.insert(position, Value::Object(note));
    }
    projection.insert("segments".to_owned(), Value::Array(segments));
    crate::turn_projection_segments::normalize_file_changes(projection);
}

/// Mirror of `lib.agent_core.output_settlement.seal_output_projection` for
/// the visible-sync slice: live blocks own recorded evidence, completion may
/// extend a text/thinking prefix or append new evidence, never replace it.
fn visible_seal_rounds(rounds: &mut Vec<Value>, additions: &[Value]) {
    for source in additions {
        let Some(row) = source.as_object() else {
            continue;
        };
        let call_id = row.get("toolCallId").and_then(Value::as_str).unwrap_or("");
        let matched = if call_id.is_empty() {
            None
        } else {
            let candidates: Vec<usize> = rounds
                .iter()
                .enumerate()
                .filter_map(|(index, existing)| {
                    let existing = existing.as_object()?;
                    (existing.get("toolCallId").and_then(Value::as_str) == Some(call_id))
                        .then_some(index)
                })
                .collect();
            (candidates.len() == 1).then(|| candidates[0])
        };
        match matched {
            None => rounds.push(Value::Object(row.clone())),
            Some(index) => {
                if let Some(existing) = rounds[index].as_object_mut() {
                    for (key, value) in row {
                        existing.entry(key.clone()).or_insert_with(|| value.clone());
                    }
                }
            }
        }
    }
}

/// Native consumer of the Python-owned token registry, generated with the
/// browser consumer. Normalize machine metadata before sealing; never dedupe
/// prose or change tool/reasoning evidence. No new execution or storage owner.
fn strip_agent_machine_tokens(text: &str) -> String {
    static PATTERNS: std::sync::OnceLock<Vec<regex::Regex>> = std::sync::OnceLock::new();
    static BLANK_LINES: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
    let patterns = PATTERNS.get_or_init(|| {
        agent_machine_tokens::MACHINE_TOKEN_PATTERNS.iter().map(|(pattern, ignore_case)| {
            regex::RegexBuilder::new(pattern).case_insensitive(*ignore_case)
                .build().expect("generated machine-token pattern must compile")
        }).collect()
    });
    let mut out = text.to_owned();
    let mut changed = false;
    for pattern in patterns {
        if pattern.is_match(&out) {
            out = pattern.replace_all(&out, "").into_owned();
            changed = true;
        }
    }
    if !changed { return out; }
    let blank_lines = BLANK_LINES.get_or_init(|| regex::Regex::new(r"\n{3,}").unwrap());
    blank_lines.replace_all(&out, "\n\n")
        .trim_matches(|ch: char| ch.is_whitespace() || matches!(ch, '\u{001c}'..='\u{001f}'))
        .to_owned()
}

fn normalize_virtual_user_live_output(projection: &mut Map<String, Value>) {
    if let Some(content) = projection.get("content").and_then(Value::as_str) {
        let normalized = strip_agent_machine_tokens(content);
        projection.insert("content".to_owned(), json!(normalized));
    }
    if let Some(segments) = projection.get_mut("segments").and_then(Value::as_array_mut) {
        for block in segments {
            if block.get("type").and_then(Value::as_str) != Some("text") { continue; }
            if let Some(object) = block.as_object_mut() {
                if let Some(text) = object.get("text").and_then(Value::as_str) {
                    let normalized = strip_agent_machine_tokens(text);
                    object.insert("text".to_owned(), json!(normalized));
                }
            }
        }
    }
}

fn visible_seal_projection(
    live: &Map<String, Value>,
    completion: &Map<String, Value>,
) -> Map<String, Value> {
    const OUTPUT_FIELDS: &[&str] = &["content", "thinking", "toolRounds", "segments"];
    let mut result = live.clone();
    for (key, value) in completion {
        if OUTPUT_FIELDS.contains(&key.as_str()) {
            continue;
        }
        match (
            value.as_object(),
            result.get(key).and_then(Value::as_object),
        ) {
            (Some(addition), Some(existing)) => {
                let mut merged = existing.clone();
                for (inner_key, inner_value) in addition {
                    merged.insert(inner_key.clone(), inner_value.clone());
                }
                result.insert(key.clone(), Value::Object(merged));
            }
            _ => {
                result.insert(key.clone(), value.clone());
            }
        }
    }
    let mut segments = result
        .get("segments")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    // Completion segment blocks append by durable identity; text/thinking
    // may extend an already recorded prefix in place.
    for (position, source) in completion
        .get("segments")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
        .iter()
        .enumerate()
    {
        let Some(block) = source.as_object() else {
            continue;
        };
        let identity = block
            .get("blockId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .map(str::to_owned)
            .unwrap_or_else(|| {
                format!(
                    "{}:completion-block-{position}",
                    block.get("type").and_then(Value::as_str).unwrap_or("")
                )
            });
        let previous = segments.iter_mut().find(|segment| {
            segment.get("blockId").and_then(Value::as_str) == Some(identity.as_str())
        });
        match previous {
            None => {
                let mut block = block.clone();
                block.insert("blockId".to_owned(), json!(identity));
                segments.push(Value::Object(block));
            }
            Some(previous) => {
                let extends = matches!(
                    (block.get("type").and_then(Value::as_str),
                     previous.get("type").and_then(Value::as_str)),
                    (Some(kind), Some(existing)) if kind == existing
                        && matches!(kind, "text" | "thinking")
                ) && block
                    .get("text")
                    .and_then(Value::as_str)
                    .is_some_and(|text| {
                        text.starts_with(previous.get("text").and_then(Value::as_str).unwrap_or(""))
                    });
                if extends {
                    if let (Some(previous), Some(text)) = (
                        previous.as_object_mut(),
                        block.get("text").and_then(Value::as_str),
                    ) {
                        previous.insert("text".to_owned(), json!(text));
                    }
                }
            }
        }
    }
    let terminal_text = segments
        .iter()
        .rev()
        .find(|block| {
            block.get("type").and_then(Value::as_str) == Some("text")
                && block.get("terminal") == Some(&json!(true))
        })
        .cloned();
    let old_content = live.get("content").and_then(Value::as_str).unwrap_or("");
    let final_content = completion
        .get("content")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            terminal_text
                .as_ref()
                .and_then(|block| block.get("text"))
                .and_then(Value::as_str)
        })
        .unwrap_or("");
    if !final_content.is_empty() && final_content != old_content {
        let recorded_index = segments.iter().rposition(|block| {
            block.get("type").and_then(Value::as_str) == Some("text")
                && block.get("text").and_then(Value::as_str) == Some(final_content)
        });
        let extends = !old_content.is_empty() && final_content.starts_with(old_content);
        let mut recorded = recorded_index;
        let mut preserved = false;
        for (index, block) in segments.iter_mut().enumerate() {
            if block.get("type").and_then(Value::as_str) != Some("text")
                || block.get("terminal") != Some(&json!(true))
            {
                continue;
            }
            if extends
                && block.get("text").and_then(Value::as_str) == Some(old_content)
                && recorded.is_none()
            {
                if let Some(object) = block.as_object_mut() {
                    object.insert("text".to_owned(), json!(final_content));
                }
                recorded = Some(index);
            }
            if recorded != Some(index) {
                if let Some(object) = block.as_object_mut() {
                    object.remove("terminal");
                    object.insert("deliverable".to_owned(), json!(false));
                }
            }
            preserved = preserved || block.get("text").and_then(Value::as_str) == Some(old_content);
        }
        if !old_content.is_empty()
            && !extends
            && !preserved
            && !segments.iter().any(|block| {
                block.get("type").and_then(Value::as_str) == Some("text")
                    && block.get("text").and_then(Value::as_str) == Some(old_content)
            })
        {
            segments.push(json!({
                "type": "text", "text": old_content, "deliverable": false,
            }));
        }
        if recorded.is_none() {
            let mut suffix = segments.len();
            let used: std::collections::BTreeSet<String> = segments
                .iter()
                .filter_map(|block| {
                    block
                        .get("blockId")
                        .and_then(Value::as_str)
                        .map(str::to_owned)
                })
                .collect();
            while used.contains(&format!("text:completion-{suffix}")) {
                suffix += 1;
            }
            segments.push(json!({
                "type": "text", "text": final_content,
                "blockId": format!("text:completion-{suffix}"),
                "terminal": true, "deliverable": true,
            }));
            recorded = Some(segments.len() - 1);
        }
        if let Some(index) = recorded {
            if let Some(object) = segments[index].as_object_mut() {
                object.insert("terminal".to_owned(), json!(true));
                object.insert("deliverable".to_owned(), json!(true));
            }
        }
        result.insert("content".to_owned(), json!(final_content));
    } else {
        result
            .entry("content".to_owned())
            .or_insert_with(|| json!(old_content));
    }
    let old_thinking = live.get("thinking").and_then(Value::as_str).unwrap_or("");
    let final_thinking = completion
        .get("thinking")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .or_else(|| {
            segments
                .iter()
                .rev()
                .find(|block| {
                    block.get("type").and_then(Value::as_str) == Some("thinking")
                        && block.get("terminal") == Some(&json!(true))
                })
                .and_then(|block| block.get("text"))
                .and_then(Value::as_str)
        })
        .unwrap_or("");
    result.insert(
        "thinking".to_owned(),
        json!(if final_thinking.starts_with(old_thinking) {
            final_thinking
        } else {
            old_thinking
        }),
    );
    if !segments.is_empty() {
        result.insert("segments".to_owned(), Value::Array(segments));
    }
    let mut rounds = result
        .get("toolRounds")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    visible_seal_rounds(
        &mut rounds,
        completion
            .get("toolRounds")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or(&[]),
    );
    result.insert("toolRounds".to_owned(), Value::Array(rounds));
    result
}

const VALID_INITIATORS: &[&str] = &[
    "human",
    "autopilot",
    "proactive",
    "timer",
    "brain",
    "peer",
    "operator",
    "swarm",
];
const ORIGIN_FIELDS: &[&str] = &[
    "blockId",
    "initiator",
    "sourceConversationId",
    "peerHuman",
    "brain",
    "autopilotRunId",
    "boardTaskId",
    "scheduledTaskId",
];
const ORIGIN_BRAIN_FIELDS: &[&str] = &[
    "epicId",
    "epicTitle",
    "originatorConv",
    "originatorTitle",
    "route",
    "method",
    "answered",
];

fn copy_named_fields(source: Option<&Value>, fields: &[&str]) -> Map<String, Value> {
    let mut out = Map::new();
    if let Some(Value::Object(source)) = source {
        for field in fields {
            if let Some(item) = source.get(*field) {
                out.insert((*field).to_owned(), item.clone());
            }
        }
    }
    out
}

fn py_or<'a>(first: Option<&'a Value>, second: Option<&'a Value>) -> Option<&'a Value> {
    match first {
        Some(value) if json_truthy(value) => Some(value),
        _ => second,
    }
}

fn py_string(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

fn visible_origin_legacy_initiator(message: &Map<String, Value>) -> String {
    if let Some(declared) = message.get("_initiator").and_then(Value::as_str) {
        if VALID_INITIATORS.contains(&declared) {
            return declared.to_owned();
        }
    }
    if message.get("_peerMessage").is_some_and(json_truthy) {
        return if message.get("_peerHuman").is_some_and(json_truthy) {
            "operator".to_owned()
        } else {
            "peer".to_owned()
        };
    }
    if message.get("_isVirtualUser").is_some_and(json_truthy)
        || message.get("_autopilotRunId").is_some_and(json_truthy)
    {
        return "autopilot".to_owned();
    }
    if message.get("_proactive").is_some_and(json_truthy) {
        return "proactive".to_owned();
    }
    if message.get("_timer").is_some_and(json_truthy) {
        return "timer".to_owned();
    }
    if message.get("_brainDispatch").is_some_and(json_truthy) {
        return "brain".to_owned();
    }
    if message.get("_swarmAutoContinue").is_some_and(json_truthy) {
        return "swarm".to_owned();
    }
    "human".to_owned()
}

/// Port of the sidecar's `_normalize_origin` (turn_projection_patch.py): the
/// projection origin block is synthesized from the legacy message flags so a
/// visible phase surfaces its true initiator on both authorities.
fn normalize_visible_origin(message: &Map<String, Value>) -> Option<Map<String, Value>> {
    let mut origin = copy_named_fields(message.get("origin"), ORIGIN_FIELDS);
    let mut brain = copy_named_fields(origin.get("brain"), ORIGIN_BRAIN_FIELDS);
    let legacy_brain = copy_named_fields(message.get("_brainEpic"), ORIGIN_BRAIN_FIELDS);
    brain.extend(legacy_brain);
    if !brain.is_empty() {
        origin.insert("brain".to_owned(), Value::Object(brain));
    } else {
        origin.remove("brain");
    }
    let initiator = match origin.get("initiator").and_then(Value::as_str) {
        Some(declared) if VALID_INITIATORS.contains(&declared) => declared.to_owned(),
        _ => visible_origin_legacy_initiator(message),
    };
    let has_legacy_origin = [
        "_initiator",
        "_peerMessage",
        "_peerHuman",
        "_fromConv",
        "_brainDispatch",
        "_brainEpic",
        "_isVirtualUser",
        "_autopilotRunId",
        "_proactive",
        "_proactiveTaskId",
        "_timer",
        "_swarmAutoContinue",
        "_boardTaskId",
        "boardTaskId",
    ]
    .into_iter()
    .any(|key| message.get(key).is_some());
    if initiator == "human" && origin.is_empty() && !has_legacy_origin {
        return None;
    }
    let block_id = origin
        .get("blockId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .unwrap_or("origin")
        .to_owned();
    origin.insert("blockId".to_owned(), json!(block_id));
    origin.insert("initiator".to_owned(), json!(initiator));
    if let Some(source_conversation_id) =
        py_or(origin.get("sourceConversationId"), message.get("_fromConv"))
    {
        if json_truthy(source_conversation_id) {
            origin.insert(
                "sourceConversationId".to_owned(),
                json!(py_string(source_conversation_id)),
            );
        }
    }
    if let Some(peer_human) = message.get("_peerHuman") {
        origin.insert("peerHuman".to_owned(), json!(json_truthy(peer_human)));
    }
    for (target, legacy) in [
        ("autopilotRunId", "_autopilotRunId"),
        ("boardTaskId", "_boardTaskId"),
        ("scheduledTaskId", "_proactiveTaskId"),
    ] {
        let value = if target == "boardTaskId" {
            py_or(
                py_or(origin.get(target), message.get(legacy)),
                message.get("boardTaskId"),
            )
        } else {
            py_or(origin.get(target), message.get(legacy))
        };
        if let Some(value) = value {
            if json_truthy(value) {
                origin.insert(target.to_owned(), json!(py_string(value)));
            }
        }
    }
    Some(origin)
}

fn visible_shape(
    raw: &Map<String, Value>,
    default_kind: &str,
    live_projection: &Map<String, Value>,
    seal_live: bool,
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
        "usageComplete",
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

        "usageRecovery",
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
    // Origin synthesis runs on the normalized message (never on the stored
    // projection), exactly like `normalize_projection_document`; when it
    // yields no block the message's own passthrough stays untouched.
    if let Some(origin) = normalize_visible_origin(&message) {
        projection.insert("origin".to_owned(), Value::Object(origin));
    }
    if seal_live {
        let mut sealed_live = live_projection.clone();
        // Preserve durable block identities on settlement and exact replay.
        if !sealed_live
            .get("segments")
            .and_then(Value::as_array)
            .is_some_and(|segments| !segments.is_empty())
        {
            visible_stable_segments(&mut sealed_live, actor);
        }
        if actor == "virtual_user" {
            normalize_virtual_user_live_output(&mut sealed_live);
        }
        projection = visible_seal_projection(&sealed_live, &projection);
        // Repair only tool blocks the seal left uncovered; never rebuild
        // already-recorded blocks (matches `_append_missing_live_tool_segments`).
        let covered: std::collections::BTreeSet<String> = projection
            .get("segments")
            .and_then(Value::as_array)
            .map(|segments| {
                segments
                    .iter()
                    .filter(|segment| {
                        segment.get("type").and_then(Value::as_str) == Some("tool_use")
                    })
                    .filter_map(|segment| {
                        segment.get("id").and_then(Value::as_str).map(str::to_owned)
                    })
                    .collect()
            })
            .unwrap_or_default();
        let missing: Vec<Value> = projection
            .get("toolRounds")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
            .into_iter()
            .enumerate()
            .filter_map(|(position, round)| {
                let round = round.as_object()?;
                if visible_round_is_synthetic(round) {
                    return None;
                }
                let call_id = round.get("toolCallId").and_then(Value::as_str)?;
                if call_id.is_empty() || covered.contains(call_id) {
                    return None;
                }
                Some(visible_tool_segment(round, position))
            })
            .collect();
        if !missing.is_empty() {
            let segments = projection
                .entry("segments".to_owned())
                .or_insert_with(|| json!([]));
            if let Some(segments) = segments.as_array_mut() {
                segments.extend(missing);
            }
        }
    } else if let (Some(Value::Array(rounds)), Some(Value::Array(live_rounds))) = (
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
    // A sealed projection already carries its recorded segment timeline;
    // only completion-only imports assemble the terminal timeline here.
    if !seal_live {
        visible_stable_segments(&mut projection, actor);
    }
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
    crate::versioned_document::put_with_blob_owner_bounded(
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
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let stored = database
        .entity_get(transaction, &loaded.key)?
        .ok_or_else(|| invalid_data("visible root Turn disappeared"))?;
    store_changed_lane_turn(database, transaction, &conversation_id, &lane_id, ordinal, stored)?;
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

/// Stable visible-phase Turn identity, mirroring the executor's
/// `visible_phase_identity`: index 0 is the root Turn itself, later phases
/// derive deterministically from the root attempt.
fn visible_turn_identity(root_turn_id: &str, attempt_id: &str, index: u64) -> String {
    if index == 0 {
        root_turn_id.to_owned()
    } else {
        Uuid::new_v5(
            &Uuid::NAMESPACE_URL,
            format!("turn-attempt:{attempt_id}:visible:{index}").as_bytes(),
        )
        .to_string()
    }
}

/// Stable visible-phase attempt identity (phases 1..; the root phase reuses
/// the root attempt and never derives one).
fn visible_attempt_identity(attempt_id: &str, index: u64) -> String {
    Uuid::new_v5(
        &Uuid::NAMESPACE_URL,
        format!("turn-attempt:{attempt_id}:visible-attempt:{index}").as_bytes(),
    )
    .to_string()
}

/// Mirror SQL `_visible_settlement`, including its historical import seam.
/// Evidence lives in the existing message contract; no wire verdict is added.
fn visible_settlement(message: &Map<String, Value>, live: bool) -> (&'static str, Value) {
    if live {
        return ("running", json!({}));
    }
    let error = message.get("error").cloned().unwrap_or(Value::Null);
    let has_error = json_truthy(&error);
    let finish = message
        .get("finishReason")
        .filter(|value| !value.is_null())
        .or_else(|| message.get("finish_reason"))
        .filter(|value| json_truthy(value))
        .map(py_string)
        .unwrap_or_default();
    let raw_stream = message
        .get("streamState")
        .filter(|value| !value.is_null())
        .or_else(|| message.get("stream_state"));
    let stream = match raw_stream {
        None | Some(Value::Null) => None,
        Some(Value::String(value)) if value.is_empty() => None,
        Some(Value::String(value)) => Some(value.as_str()),
        _ => Some("unknown"),
    };
    let task_status = message.get("status").and_then(Value::as_str).unwrap_or("");
    let internal_abort = message
        .get("_abort_reason")
        .and_then(Value::as_str)
        .is_some_and(|reason| {
            matches!(
                reason,
                "storage_authority_integrity"
                    | "storage_frame_overflow"
                    | "turn_attempt_stale_fence"
                    | "conversation_attempt_stale_fence"
            )
        });
    let (status, cause) = if !message.contains_key("finishReason")
        && !message.contains_key("streamState")
    {
        // SQL retains completion-only imports that predate terminal evidence.
        if has_error {
            ("failed", "generation_error")
        } else {
            ("completed", "phase_completed")
        }
    } else if internal_abort {
        ("failed", "internal_fence_abort")
    } else if message.get("aborted").is_some_and(json_truthy)
        || task_status == "aborted"
        || stream == Some("client_aborted")
        || matches!(finish.as_str(), "aborted" | "interrupted")
    {
        ("interrupted", "user_abort")
    } else if matches!(
        stream,
        Some(
            "premature_close"
                | "malformed_stream"
                | "semantic_progress_timeout"
                | "no_actionable_output"
                | "empty_response"
                | "tool_payload_missing"
                | "gateway_limit_response"
        )
    ) || matches!(finish.as_str(), "premature_close" | "abnormal_stop")
    {
        ("failed", "provider_stream_error")
    } else if has_error || task_status == "error" || matches!(finish.as_str(), "error" | "timeout")
    {
        ("failed", "generation_error")
    } else if matches!(
        finish.as_str(),
        "length"
            | "max_tokens"
            | "context_length"
            | "content_filter"
            | "budget_exceeded"
            | "incomplete"
            | "max_turns"
    ) {
        ("truncated", finish.as_str())
    } else if matches!(stream, None | Some("provider_finished"))
        && matches!(
            finish.as_str(),
            "stop"
                | "done"
                | "completed"
                | "end_turn"
                | "tool_calls"
                | "tool_use"
                | "function_call"
        )
    {
        ("completed", "provider_finished")
    } else {
        ("failed", "completion_evidence_missing")
    };
    (
        status,
        json!({
            "outcome": status, "cause": cause,
            "providerFinishReason": message.get("finishReason"),
            "error": error, "resumeOptions": []
        }),
    )
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
    // SQL sidecar parity: a settled attempt owns no further visible phases.
    if !attempt_is_live(&attempt.document) {
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
    // Offset-window parity with the SQL sidecar: phases before `start_index`
    // are already committed; this window's first child links after them and
    // the returned identity list covers the whole committed prefix.
    let mut previous_id = if request.start_index == 0 {
        root.document
            .get("parentTurnId")
            .and_then(Value::as_str)
            .map(str::to_owned)
    } else {
        Some(visible_turn_identity(
            &request.root_turn_id,
            &request.attempt_id,
            request.start_index - 1,
        ))
    };
    let mut visible_ids: Vec<String> = (0..request.start_index)
        .map(|index| visible_turn_identity(&request.root_turn_id, &request.attempt_id, index))
        .collect();
    visible_ids.reserve(request.messages.len());
    let empty_live_projection = Map::new();
    let mut root_public = public_turn(root.document.clone(), execution_epoch)?;
    let mut related = if request.start_index > 0 {
        vec![root_public.clone()]
    } else {
        Vec::with_capacity(request.messages.len())
    };
    let mut changed = false;
    let mut root_after = root_projection_before.clone();
    let mut root_revision = root_base_revision;
    for (window_position, message) in request.messages.iter().enumerate() {
        let index = request.start_index + window_position as u64;
        // The root (index 0) seals its own live projection against the
        // completion facts; child phases shape from the root's live state
        // through the legacy field-inheritance seam. A live admission shapes
        // its shell only — never sealed, never inheriting completion fields.
        let (phase_status, settlement) = visible_settlement(message, request.live);
        let (actor, kind, mut projection) = visible_shape(
            message,
            &request.default_kind,
            if request.live {
                &empty_live_projection
            } else {
                &root_projection_before
            },
            index == 0 && !request.live,
        );
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
                // Root conversion changes the projection before any token is
                // delivered. Keep the bounded restart sweep's byte witness in
                // this transaction, just like ordinary turn.event.record.
                let created_at_ms = attempt.document.get("createdAt")
                    .and_then(Value::as_u64)
                    .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
                let projection_bytes = serde_json::to_vec(&projection)
                    .map_err(|_| invalid_data("visible projection cannot be encoded"))?.len();
                database.entity_put(
                    transaction,
                    recovery_index_key(transaction, created_at_ms, &request.attempt_id)?,
                    recovery_index_value(&attempt.document, projection_bytes)?,
                )?;
                root_after = projection;
                changed = true;
            }
            related.push(root_public.clone());
            continue;
        }
        let turn_id = visible_turn_identity(&request.root_turn_id, &request.attempt_id, index);
        let child_attempt_id = visible_attempt_identity(&request.attempt_id, index);
        projection = crate::turn_projection_segments::stabilize_for_patch(
            &projection, &actor, phase_status,
        )?;
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
            let child_attempt = load_attempt_for_update(database, transaction, &child_attempt_id)?
                .ok_or_else(|| conflict("visible phase attempt is missing"))?;
            if document.get("currentAttemptId").and_then(Value::as_str) != Some(child_attempt_id.as_str())
                || document.get("laneId").and_then(Value::as_str) != Some(lane_id.as_str())
                || document.get("parentTurnId").and_then(Value::as_str) != previous_id.as_deref()
                || child_attempt.conversation_id != request.conversation_id
                || child_attempt.document.get("turnId").and_then(Value::as_str) != Some(turn_id.as_str())
                || child_attempt
                    .document
                    .get("_config")
                    .and_then(|config| config.get("flowRootAttemptId"))
                    .and_then(Value::as_str)
                    != Some(request.attempt_id.as_str())
            {
                return Err(conflict(
                    "visible phase attempt was superseded or belongs to another parent",
                ));
            }
            let status = document.get("status").and_then(Value::as_str).unwrap_or("");
            if matches!(status, "pending" | "running") && request.live {
                if !attempt_is_live(&child_attempt.document) {
                    return Err(conflict("visible phase attempt lost authority"));
                }
                public_turn(document, execution_epoch)?
            } else {
                let previous = document
                    .get("projection")
                    .and_then(Value::as_object)
                    .cloned()
                    .unwrap_or_default();
                // Refresh cumulative live mirrors before the first seal only.
                // A terminal replay must compare the already-sealed evidence,
                // not generate additional placeholder blocks on that evidence.
                let live_baseline = if matches!(status, "pending" | "running") {
                    crate::turn_projection_segments::stabilize_for_patch(&previous, &actor, "running")
                        .unwrap_or_else(|_| previous.clone())
                } else {
                    previous.clone()
                };
                let (_, _, sealed) = visible_shape(message, &request.default_kind, &live_baseline, true);
                let sealed =
                    crate::turn_projection_segments::stabilize_for_patch(&sealed, &actor, phase_status)?;
                if !matches!(status, "pending" | "running") {
                    if request.live
                        || status != phase_status
                        || sealed != previous
                        || document
                            .get("settlement")
                            .and_then(|value| value.get("cause"))
                            != settlement.get("cause")
                        || child_attempt.document.get("status").and_then(Value::as_str) != Some(status)
                    {
                        return Err(conflict("visible phase already settled differently"));
                    }
                    public_turn(document, execution_epoch)?
                } else {
                    // Keep the normal terminal authority path: attempt fence,
                    // projection head retirement, indexes, timing, and sync event
                    // commit atomically with the rest of this visible batch.
                    let result: Value = serde_json::from_slice(&event_record(
                        database,
                        transaction,
                        &EventRecordRequest {
                            model_continuation: Value::Null,
                            attempt_id: child_attempt_id.clone(),
                            task_id: String::new(),
                            terminal: true,
                            status: phase_status.to_owned(),
                            slim: false,
                            content: String::new(),
                            thinking: String::new(),
                            projection: Value::Object(sealed),
                            projection_patch: None,
                            projection_segments_stable: true,
                            settlement: settlement.as_object().cloned().unwrap_or_default(),
                            error: Map::new(),
                            event_type: "terminal_settlement".to_owned(),
                            event_payload: Map::new(),
                            background_command_ack: None,
                            task_event: None,
                            preceding_task_events: Vec::new(),
                            flow_root_attempt_id: request.attempt_id.clone(),
                            now_ms: request.updated_at_ms,
                        },
                    )?)
                    .map_err(|_| invalid_data("visible settlement response is malformed"))?;
                    if result.get("applied") != Some(&Value::Bool(true)) {
                        return Err(conflict("visible phase settlement lost authority"));
                    }
                    let stored = database
                        .entity_get(transaction, &key)?
                        .ok_or_else(|| invalid_data("settled visible child disappeared"))?;
                    public_turn(
                        materialize_turn(database, transaction, &stored)?,
                        execution_epoch,
                    )?
                }
            }
        } else {
            let head_key = lane_state_key(transaction, &request.conversation_id, &lane_id)?;
            let lane_state =
                read_lane_state(database, transaction, &request.conversation_id, &lane_id)?;
            let ordinal = lane_state
                .as_ref()
                .map(|state| state.head)
                .unwrap_or(0)
                .checked_add(1)
                .ok_or_else(|| invalid_data("visible lane ordinal overflows"))?;
            let count = lane_count_of(
                database,
                transaction,
                &request.conversation_id,
                &lane_id,
                lane_state.as_ref(),
            )?
            .checked_add(1)
            .ok_or_else(|| invalid_data("visible lane count overflows"))?;
            let (settled_at, event_type) = if request.live {
                (Value::Null, "status_changed")
            } else {
                (json!(request.updated_at_ms), "terminal_settlement")
            };
            let parent_task_id = attempt
                .document
                .get("taskId")
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_owned();
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
                ("status".to_owned(), json!(phase_status)),
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
            database.entity_put(
                transaction,
                head_key,
                encode_lane_state(&LaneState {
                    head: ordinal,
                    count: Some(count),
                    last_turn_id: Some(turn_id.clone()),
                }),
            )?;
            let child_attempt = Map::from_iter([
                ("attemptId".to_owned(), json!(child_attempt_id)),
                ("conversationId".to_owned(), json!(request.conversation_id)),
                ("turnId".to_owned(), json!(turn_id)),
                (
                    "commandId".to_owned(),
                    json!(format!("run:{}:visible:{index}", request.attempt_id)),
                ),
                ("taskId".to_owned(), json!(parent_task_id)),
                ("operation".to_owned(), json!("generate")),
                ("status".to_owned(), json!(phase_status)),
                ("baseProjectionRevision".to_owned(), json!(0)),
                ("resumeAnchor".to_owned(), json!({})),
                ("createdAt".to_owned(), json!(request.updated_at_ms)),
                ("startedAt".to_owned(), json!(request.updated_at_ms)),
                ("settledAt".to_owned(), settled_at),
                ("_dispatchMode".to_owned(), json!("")),
                ("_queueId".to_owned(), json!("")),
                ("_queueState".to_owned(), json!("")),
                (
                    "_config".to_owned(),
                    json!({"runId":run_id, "flowRootAttemptId":request.attempt_id}),
                ),
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
                    event_type,
                    payload: json!({"status":phase_status, "settlement":settlement}),
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
            > MAX_LEGACY_DOCUMENT_BYTES
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
            .filter(|bytes| *bytes <= MAX_LEGACY_DOCUMENT_BYTES)
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
    let (_, document_json) = crate::versioned_document::materialize_stored_document_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
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

    if crate::model_continuation::model_projections_equal(&previous_projection, &next_projection) {
        let context_revision = model_context_revision(&document)?;
        let offset = next_projection_revision.checked_sub(context_revision)
            .ok_or_else(|| invalid_data("Turn context display offset overflow"))?;
        document.insert("_modelContextDisplayOffset".to_owned(), json!(offset));
    }
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
    crate::versioned_document::put_with_blob_owner_bounded(
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
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let staged = database
        .entity_get(
            transaction,
            &turn_key(transaction, &request.conversation_id, &request.turn_id)?,
        )?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    store_changed_lane_turn(database, transaction, &request.conversation_id, &lane_id, ordinal, staged)?;
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
    let mut pruned = 0;
    for (age_key, attempt_id_bytes) in stale {
        let (deleted_at_ms, conversation_id, turn_id) = decode_age_index_identity(&age_key)?;
        if deleted_at_ms >= cutoff_ms {
            return Err(invalid_data("turn tombstone age scan escaped its bound"));
        }
        // A restored conversation may still own detached old event bytes.
        // Keep their identities reserved until its pinned cleanup has finished.
        if event_retirement::pending(database, transaction, &conversation_id)? {
            continue;
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
        pruned += 1;
    }
    Ok(pruned)
}

fn store_activity_sync_event(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
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
        Some(turn_id),
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
                .map_err(|_| invalid_data("activity sync event cannot be encoded"))?,
            expected_version: Some(0),
            updated_at_ms: occurred_at_ms,
        },
    )?;
    store_sync_retention_indexes(database, transaction, &event)?;
    database.entity_put(transaction, head_key, sequence.to_le_bytes().to_vec())?;
    Ok(())
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
        crate::versioned_document::put_with_blob_owner_bounded(
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
            transaction.owner_user_id(),
            crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
        )?;
        let staged = database
            .entity_get(transaction, &key)?
            .ok_or_else(|| invalid_data("staged retained Turn disappeared"))?;
        store_changed_lane_turn(database, transaction, &request.conversation_id,
            "main", row.ordinal, staged)?;
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
                preserve_activity: false,
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
    crate::versioned_document::put_with_blob_owner_bounded(
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
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    database.entity_put(
        transaction,
        summary_claim,
        transaction.owner_user_id().to_be_bytes().to_vec(),
    )?;
    let staged = database
        .entity_get(transaction, &summary_key)?
        .ok_or_else(|| invalid_data("staged summary Turn disappeared"))?;
    store_changed_lane_turn(database, transaction, &request.conversation_id,
        "main", summary_ordinal, staged)?;
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
    let lane_state = read_lane_state(database, transaction, &request.conversation_id, "main")?;
    let count = lane_count_of(
        database,
        transaction,
        &request.conversation_id,
        "main",
        lane_state.as_ref(),
    )?
    .checked_add(1)
    .ok_or_else(|| invalid_data("Turn lane count overflow"))?;
    database.entity_put(
        transaction,
        lane_state_key(transaction, &request.conversation_id, "main")?,
        encode_lane_state(&LaneState {
            head: lane_state.as_ref().map(|state| state.head).unwrap_or(0),
            count: Some(count),
            last_turn_id: lane_state.and_then(|state| state.last_turn_id.clone()),
        }),
    )?;
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
    store_activity_sync_event(
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
    // Turn deletion discards the conversation's derived model continuations
    // (legacy `_delete_turn_row_set` performs the same cleanup when the
    // deleted closure is non-empty).
    if !rows.is_empty() {
        crate::model_continuation::delete_conversation(database, transaction, conversation_id)?;
    }
    // Deletion changes the visible message stream: the archive falls back
    // to the exact lane read until appends rebuild it.
    if !rows.is_empty() {
        crate::transcript_archive::invalidate(database, transaction, conversation_id)?;
    }
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
        let lane_state = read_lane_state(database, transaction, conversation_id, &lane_id)?;
        let count = lane_count_of(
            database,
            transaction,
            conversation_id,
            &lane_id,
            lane_state.as_ref(),
        )?;
        let remaining = count
            .checked_sub(deleted_count)
            .ok_or_else(|| invalid_data("turn lane count underflow"))?;
        // The fused record stays with an exact count of zero so the head
        // ordinal remains monotonic across deletion gaps (the legacy shape
        // deleted the standalone count key, which the fused format subsumes).
        // The tail identity is cleared on any lane deletion: the next
        // append's one legacy rescan re-seeds the latest surviving
        // predecessor instead of trusting a possibly deleted tail.
        database.entity_put(
            transaction,
            lane_state_key(transaction, conversation_id, &lane_id)?,
            encode_lane_state(&LaneState {
                head: lane_state.as_ref().map(|state| state.head).unwrap_or(0),
                count: Some(remaining),
                last_turn_id: None,
            }),
        )?;
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
        preserve_activity: false,
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
        preserve_activity: false,
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
    get_public_value(database, transaction, conversation_id, turn_id)?
        .map(|value| encode_response(&value)).transpose()
}

/// The caller owns this freshly materialized public projection. Byte-oriented
/// callers still use get; context composition can retain the parsed tree.
fn get_public_value(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_id: &str,
) -> io::Result<Option<Value>> {
    let key = turn_key(transaction, conversation_id, turn_id)?;
    let Some(stored) = database.entity_get(transaction, &key)? else {
        return Ok(None);
    };
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    public_turn(
        materialize_turn(database, transaction, &stored)?,
        execution_epoch,
    ).map(Some)
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
    Ok(Some(encoded))
}

pub(crate) fn list(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: Option<&str>,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<u8>> {
    encode_response(&Value::Array(list_values_until(
        database,
        transaction,
        conversation_id,
        lane_id,
        deadline_elapsed,
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
            .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "turn delta exceeds its bounded streamed payload",
                )
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
                .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES)
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "turn delta exceeds its bounded streamed payload",
                    )
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

pub(crate) fn list_values_until(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: Option<&str>,
    deadline_elapsed: &mut dyn FnMut() -> bool,
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
            // A full turn list has no resumption cursor, so deadline
            // exhaustion fails loud (`deadline_elapsed`) rather than handing
            // the caller a silently incomplete history.
            if deadline_elapsed() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "turn list read deadline elapsed",
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
                .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES)
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "turn list exceeds its bounded streamed payload",
                    )
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
    let mut source_turns = 0usize;
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
            let ordinal = document
                .get("ordinal")
                .and_then(Value::as_u64)
                .ok_or_else(|| invalid_data("turn search source ordinal is malformed"))?;
            source_turns += 1;
            crate::search_text::chunks_for_turn(&turn_id, ordinal, actor, projection, &mut turns);
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
        source_turns,
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

/// Materializes the lane window `[start, end)` through the exact
/// per-document path — the same chain the bounded window read uses.
pub(crate) fn materialize_lane_range(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    start: u64,
    end: u64,
    execution_epoch: u64,
) -> io::Result<Vec<Value>> {
    let prefix = lane_prefix(conversation_id, "main")?;
    let (scan_start, scan_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    if start > end {
        return Err(invalid_input("lane range starts after its end"));
    }
    if end > MAX_LIST_ROWS as u64 {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory, "turn list exceeds 10000 rows"));
    }
    let mut messages = Vec::with_capacity((end - start) as usize);
    let mut cursor = scan_start;
    let mut position = 0_u64;
    while position < end {
        let page_rows = INDEX_PAGE_ROWS.min((end - position) as usize);
        let rows = database.entity_scan(transaction, &cursor, &scan_end, page_rows)?;
        if rows.is_empty() { break; }
        let last = &rows.last().expect("nonempty lane page").0;
        cursor = after_key(last, transaction, TURN_LANE_INDEX_NAMESPACE)?;
        for (_, stored) in rows {
            if position >= start {
                let turn = public_turn(
                    materialize_turn(database, transaction, &stored)?, execution_epoch,
                )?;
                messages.push(legacy_message(turn)?);
            }
            position += 1;
        }
    }
    if messages.len() as u64 != end - start {
        return Err(invalid_data("lane window is shorter than its count"));
    }
    Ok(messages)
}

fn legacy_message(turn: Value) -> io::Result<Value> {
    // This is a private materialized Turn, never a shared/cached document.
    // Move its projection and metadata into the compatibility response.
    let Value::Object(mut turn) = turn else {
        return Err(invalid_data("turn projection is malformed"));
    };
    let Some(Value::Object(mut message)) = turn.remove("projection") else {
        return Err(invalid_data("turn projection is malformed"));
    };
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
            turn.remove(source).unwrap_or(Value::Null),
        );
    }
    message.insert("_commandPending".to_owned(), Value::Null);
    if !message.get("timestamp").is_some_and(python_truthy) {
        message.insert(
            "timestamp".to_owned(),
            turn.remove("createdAt").unwrap_or(Value::from(0)),
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
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<(Vec<Value>, usize, usize, usize)> {
    if window == 0 {
        return legacy_messages_full(
            database,
            transaction,
            conversation_id,
            window,
            before_sequence,
            deadline_elapsed,
        );
    }
    // Bounded window: read only the windowed lane-index range and
    // materialize at most `window` turn documents, instead of
    // materializing the full lane and slicing afterwards. The lane's
    // maintained count supplies `total` in O(1); order (never ordinal
    // arithmetic) selects the window, so gaps in ordinal space are
    // irrelevant. Any disagreement between the count and the lane falls
    // back to the exact historical full read.
    let prefix = lane_prefix(conversation_id, "main")?;
    let (scan_start, scan_end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        TURN_LANE_INDEX_NAMESPACE,
        &prefix,
    )?;
    // Fused lane state first; the legacy count key stays the fallback, and
    // only its ABSENCE (predating the maintained count) takes the exact
    // legacy path — the same gate as before the fusion.
    let lane_state = read_lane_state(database, transaction, conversation_id, "main")?;
    let count_value = match lane_state.and_then(|state| state.count) {
        Some(count) => Some(count),
        None => database
            .entity_get(
                transaction,
                &lane_count_key(transaction, conversation_id, "main")?,
            )?
            .map(|value| decode_u64(Some(value), "Turn lane count is malformed"))
            .transpose()?,
    };
    let newest = database.entity_scan_reverse(transaction, &scan_start, &scan_end, 1)?;
    if newest.is_empty() {
        return crate::frozen_transcript::messages(
            database,
            transaction,
            conversation_id,
            window,
            before_sequence,
        );
    }
    let Some(lane_total) = count_value else {
        // Lane rows predate the maintained count: exact legacy path.
        return legacy_messages_full(
            database,
            transaction,
            conversation_id,
            window,
            before_sequence,
            deadline_elapsed,
        );
    };
    let total =
        usize::try_from(lane_total).map_err(|_| invalid_data("turn lane count overflows usize"))?;
    if total == 0 {
        // A nonempty lane with a zero count disagrees with itself.
        return legacy_messages_full(
            database,
            transaction,
            conversation_id,
            window,
            before_sequence,
            deadline_elapsed,
        );
    }
    let end = before_sequence.map_or(total, |before| {
        usize::try_from(before.max(0))
            .unwrap_or(usize::MAX)
            .min(total)
    });
    let start = end.saturating_sub(window);
    let take = end - start;
    if take == 0 {
        return Ok((Vec::new(), total, start, end));
    }
    let skip = total - end;
    if skip
        .checked_add(take)
        .is_none_or(|rows| rows > MAX_LIST_ROWS)
    {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "turn list exceeds 10000 rows",
        ));
    }
    let scanned =
        database.entity_scan_reverse(transaction, &scan_start, &scan_end, skip + take + 1)?;
    // Normal outcomes: `start > 0` yields skip+take+1 entries (older rows
    // legitimately exist beyond the window); `start == 0` yields exactly
    // skip+take. Fewer than skip+take means the count overstates the lane
    // beyond the window's need; an extra entry at `start == 0` means it
    // understates the lane. Either disagreement takes the authoritative
    // full read.
    if scanned.len() < skip + take || (start == 0 && scanned.len() > skip + take) {
        return legacy_messages_full(
            database,
            transaction,
            conversation_id,
            window,
            before_sequence,
            deadline_elapsed,
        );
    }
    // Archive fast path: the same window out of compact page documents
    // (fenced on the maintained lane count the exact path uses).
    {
        let lane_count = read_lane_state(database, transaction, conversation_id, "main")?
            .and_then(|state| state.count);
        if let Some(lane_count) = lane_count {
            if let Some((messages, archived_total, archived_start, archived_end)) =
                crate::transcript_archive::read_window(
                    database,
                    transaction,
                    conversation_id,
                    window,
                    before_sequence,
                    lane_count,
                )?
            {
                if archived_total == total && archived_start == start && archived_end == end {
                    return Ok((messages, total, start, end));
                }
            }
        }
    }
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let mut window_entries = scanned;
    window_entries.truncate(skip + take);
    window_entries.drain(..skip);
    window_entries.reverse();
    let mut messages = Vec::with_capacity(take);
    let mut response_bytes = 2_usize;
    for (_, stored) in window_entries {
        // Windowed reads resume through `before_sequence`; deadline
        // exhaustion still fails loud rather than truncating the window.
        if deadline_elapsed() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "turn list read deadline elapsed",
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
                    + usize::from(!messages.is_empty()),
            )
            .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "turn list exceeds its bounded streamed payload",
                )
            })?;
        messages.push(legacy_message(turn)?);
    }
    Ok((messages, total, start, end))
}

fn legacy_messages_full(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    window: usize,
    before_sequence: Option<i64>,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<(Vec<Value>, usize, usize, usize)> {
    if deadline_elapsed() {
        return Err(io::Error::new(io::ErrorKind::TimedOut, "turn list read deadline elapsed"));
    }
    let lane_count = read_lane_state(database, transaction, conversation_id, "main")?
        .and_then(|state| state.count);
    if let Some(lane_count) = lane_count.filter(|count| *count <= MAX_LIST_ROWS as u64) {
        if let Some(mut turns) = crate::transcript_archive::read_sealed(
            database,
            transaction,
            conversation_id,
            lane_count,
        )? {
            let sealed = turns.len() as u64;
            let total = lane_count as usize;
            let end = before_sequence.map_or(total, |before| {
                usize::try_from(before.max(0))
                    .unwrap_or(usize::MAX)
                    .min(total)
            });
            // Zero is the public full-history sentinel, not an empty window.
            let start = if window == 0 { 0 } else { end.saturating_sub(window) };
            if (start as u64) >= sealed {
                // The whole window is in the exact-read tail anyway.
            } else {
                if end as u64 > sealed {
                    let execution_epoch = crate::conversation_header::execution_epoch(
                        database,
                        transaction,
                        conversation_id,
                    )?;
                    let mut tail = materialize_lane_range(
                        database,
                        transaction,
                        conversation_id,
                        sealed,
                        end as u64,
                        execution_epoch,
                    )?;
                    turns.append(&mut tail);
                }
                if deadline_elapsed() {
                    return Err(io::Error::new(io::ErrorKind::TimedOut, "turn list read deadline elapsed"));
                }
                let response_bytes = serde_json::to_vec(&turns)
                    .map_err(|_| invalid_data("turn response cannot be encoded"))?.len();
                if response_bytes > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
                    return Err(io::Error::new(io::ErrorKind::OutOfMemory,
                        "turn list exceeds its bounded streamed payload"));
                }
                turns.truncate(end);
                if start != 0 {
                    turns.drain(..start);
                }
                return Ok((turns, total, start, end));
            }
        }
    }
    let turns = list_values_until(
        database,
        transaction,
        conversation_id,
        Some("main"),
        deadline_elapsed,
    )?;
    if turns.is_empty() {
        return crate::frozen_transcript::messages(
            database,
            transaction,
            conversation_id,
            window,
            before_sequence,
        );
    }
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
    let messages = turns
        .into_iter()
        .skip(start)
        .take(end - start)
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
    let lane_state = read_lane_state(database, transaction, conversation_id, "main")?;
    let lane_count = lane_count_of(
        database,
        transaction,
        conversation_id,
        "main",
        lane_state.as_ref(),
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

/// Scoped Attempt rows are detached by range retirement. Legacy random-key
/// rows predate epochs and must stay inaccessible after delete/restore.
fn attempt_conversation_is_active(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    legacy: bool,
) -> io::Result<bool> {
    Ok(crate::conversation_header::sync_header(database, transaction, conversation_id)?.is_some()
        && (!legacy || crate::conversation_header::execution_epoch(database, transaction, conversation_id)? == 0))
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
    if !attempt_conversation_is_active(database, transaction, &conversation_id,
        located_conversation_id.is_none())? {
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
    let Some(mut loaded) = load_current_turn_header_for_attempt(
        database, transaction, attempt_id, attempt, conversation_id,
    )? else { return Ok(None); };
    if let Some(head) = &loaded.projection_head {
        loaded.verified_stable_segments =
            materialize_projection_head(database, transaction, &mut loaded.document, head)?;
    }
    Ok(Some(loaded))
}

fn load_current_turn_header_for_attempt(
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
    let (_, physical_json) = crate::versioned_document::materialize_stored_document_bounded(
        database,
        transaction.tenant_id(),
        transaction.owner_user_id(),
        &stored,
        DOCUMENT_IDENTITY,
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let physical_document = decode_turn_value(&physical_json)?;
    let projection_head = projection_head_from_document(&physical_document)?;
    let document = physical_document;
    if document.get("currentAttemptId").and_then(Value::as_str) != Some(attempt_id) {
        return Ok(None);
    }
    Ok(Some(LoadedTurnForAttempt {
        key,
        physical_version,
        document,
        projection_head,
        verified_stable_segments: None,
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
        if current.text_tail.is_none() && next_count <= MAX_TURN_PROJECTION_HEAD_PATCHES
            && next_bytes <= MAX_TURN_PROJECTION_PATCH_BYTES
        {
            let next = ProjectionHeadDescriptor {
                head_id: current.head_id.clone(),
                attempt_id: current.attempt_id.clone(),
                base_revision: current.base_revision,
                patch_count: next_count,
                patch_bytes: next_bytes,
                text_tail: None, frame_witness: None,
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
                text_tail: None, frame_witness: None,
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
    crate::versioned_document::put_with_blob_owner_bounded(
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
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let staged = database
        .entity_get(transaction, &loaded.key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    store_changed_lane_turn(database, transaction, conversation_id, &lane_id, ordinal, staged)?;
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
        let (_, turn_json) = crate::versioned_document::materialize_stored_document_bounded(
            database,
            transaction.tenant_id(),
            owner_user_id,
            &stored_turn,
            DOCUMENT_IDENTITY,
            crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
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
            .is_none_or(|bytes| bytes > MAX_LEGACY_DOCUMENT_BYTES)
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
            format!(
                "storage_payload_too_large: worker job payload exceeds {MAX_WORKER_JOB_PAYLOAD_BYTES} bytes"
            ),
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
    crate::versioned_document::put_with_blob_owner_bounded(
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
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let staged = database
        .entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    store_changed_lane_turn(database, transaction, conversation_id, &lane_id, ordinal, staged)?;
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
    attempt_get_with_config(database,transaction,attempt_id,false)
}

pub(crate) fn attempt_get_with_config(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    include_execution_config: bool,
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
        && !attempt_conversation_is_active(database, transaction, conversation_id, true)?
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
    let mut result=public_attempt(document)?;
    if include_execution_config {
        let config=document.get("_config").and_then(Value::as_object)
            .ok_or_else(||invalid_data("attempt execution config is malformed"))?;
        result["config"]=Value::Object(config.clone());
    }
    encode_response(&result).map(Some)
}

/// Existence probe for the invocation registrar's fallback parent proof: an
/// attempt carrying this exact task ID was born inside this conversation.
/// Ports the legacy `storage_generation_attempts JOIN storage_conversations`
/// lookup; owner scoping comes from the entity key, and the caller has
/// already required the conversation to be active.
pub(crate) fn attempt_exists_for_task_in_conversation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    task_id: &str,
    conversation_id: &str,
) -> io::Result<bool> {
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
                "invocation parent candidates exceed their bound",
            ));
        }
        let indexed = decode_attempt_timing_index(raw)?;
        if indexed.get("taskId").and_then(Value::as_str) != Some(task_id) {
            return Err(invalid_data("attempt task index escaped its prefix"));
        }
        if indexed.get("conversationId").and_then(Value::as_str) == Some(conversation_id) {
            return Ok(true);
        }
    }
    Ok(false)
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
    // Validate the complete superseded tail before staging ANY mutations,
    // especially submitted-input edits. Resume keeps the target checkpoint;
    // only later same-lane history and its rooted branches are discarded.
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
    let discarded_rows = collect_deletion_closure(
        database,
        transaction,
        &request.conversation_id,
        &tail,
        &[],
        None,
    )?;

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
        ) {
            return Err(conflict("generated turn has no editable submitted parent"));
        }
        let parent_revision = parent
            .get("projectionRevision")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("submitted turn projection revision is malformed"))?;
        let next_projection = if request.operation == "regenerate" {
            // Regenerate replaces the submitted projection outright, fenced on
            // the revision the editor saw.
            if Some(parent_revision) != request.expected_input_projection_revision {
                return Err(conflict("submitted turn changed since editing began"));
            }
            input_update.clone()
        } else {
            // Resume operations (continue/checkpoint_resume/answer_guidance)
            // surgically rebind the contextSnapshot rail onto the CURRENT
            // submitted projection: submitted content stays immutable
            // historical evidence and the rail snapshot is last-writer-wins
            // display state — no client revision fence (the attempt's own
            // projection CAS already gates the operation). Mirrors the
            // sidecar's _resume_ctx_rebound_projection.
            let mut merged = parent
                .get("projection")
                .and_then(Value::as_object)
                .cloned()
                .ok_or_else(|| invalid_data("submitted turn projection is malformed"))?;
            merged.remove("_ctx");
            match input_update.get("contextSnapshot") {
                None | Some(Value::Null) => {
                    merged.remove("contextSnapshot");
                }
                Some(Value::Object(ctx)) => {
                    let snapshot =
                        ctx.get("snapshot")
                            .and_then(Value::as_object)
                            .ok_or_else(|| {
                                invalid_input("contextSnapshot must carry a snapshot object")
                            })?;
                    let block_id = ctx
                        .get("blockId")
                        .and_then(Value::as_str)
                        .filter(|value| !value.is_empty())
                        .unwrap_or("turn-context");
                    let block_id: String = block_id.chars().take(80).collect();
                    merged.insert(
                        "contextSnapshot".to_owned(),
                        json!({"blockId": block_id, "snapshot": snapshot}),
                    );
                }
                Some(_) => {
                    return Err(invalid_input("contextSnapshot must be an object or null"));
                }
            }
            merged
        };
        let update = ProjectionUpdateRequest {
            preserve_activity: false,
            conversation_id: request.conversation_id.clone(),
            turn_id: parent_turn_id.to_owned(),
            projection_json: serde_json::to_vec(&next_projection)
                .map_err(|_| invalid_input("submitted projection cannot be encoded"))?,
            expected_projection_revision: parent_revision,
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

    let (deleted_turn_ids, deleted_main_turns) = if discarded_rows.is_empty() {
        (Vec::new(), 0)
    } else {
        apply_deletion_rows(
            database,
            transaction,
            &request.conversation_id,
            &discarded_rows,
            request.created_at_ms,
        )?
    };
    if request.operation == "regenerate" {
        projection = Map::from_iter([
            ("content".to_owned(), Value::String(String::new())),
            ("thinking".to_owned(), Value::String(String::new())),
            ("segments".to_owned(), Value::Array(Vec::new())),
            ("toolRounds".to_owned(), Value::Array(Vec::new())),
        ]);
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
    if request.operation == "checkpoint_resume" {
        // The accepted anchor clears the unfinished prose tail, while its
        // completed tool receipts remain visible. Materialize those blocks
        // at this producer boundary before publishing a pending attempt;
        // readers must keep rejecting malformed committed projections.
        projection = crate::turn_projection_segments::stabilize_for_patch(
            &projection, next_actor, "running",
        )?;
    }
    let mut accepted_config = request.config.as_object().cloned()
        .ok_or_else(|| invalid_input("attempt config must be an object"))?;
    accepted_config.insert("_resumeSource".to_owned(), json!({
        "attemptId": current_attempt_id.as_deref().unwrap_or(""),
        "taskId": current_attempt.as_ref().and_then(|value| value.get("taskId")).and_then(Value::as_str).unwrap_or(""),
        "status": current_attempt.as_ref().and_then(|value| value.get("status")).and_then(Value::as_str).unwrap_or(""),
        "cause": settlement.get("cause").or_else(|| settlement.get("reason")).and_then(Value::as_str).unwrap_or("")
    }));
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
        ("_config".to_owned(), Value::Object(accepted_config)),
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
    response.insert(
        "deletedTurnIds".to_owned(),
        Value::Array(deleted_turn_ids.into_iter().map(Value::String).collect()),
    );
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
        && !attempt_conversation_is_active(database, transaction, conversation_id, true)?
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
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    attempt_id: &str, task_id: &str, now_ms: u64,
) -> io::Result<Vec<u8>> {
    attempt_start_with_automation(database, transaction, attempt_id, task_id, now_ms, None)
}

pub(crate) fn attempt_start_with_automation(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    attempt_id: &str,
    task_id: &str,
    now_ms: u64,
    automation_context: Option<&crate::automation_events::TaskStartedContext>,
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
    if let Some(context) = automation_context {
        crate::automation_events::enqueue_task_start(database, transaction, context, attempt_id, task_id,
            &loaded.conversation_id, &turn_id,
            attempt["startedAt"].as_u64().ok_or_else(|| invalid_data("missing actual task start"))?)?;
    }
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

// Native subset of _model_continuation.settle_continuation. Validation and
// negative settlement outcomes are unchanged; a valid candidate is handed to
// the durable checkpoint path whose ordinal-frontier CAS is atomic with the
// settlement transaction. A stored value is acknowledged only after the
// frontier admits it.
fn terminal_continuation_candidate<'a>(
    status: &str,
    candidate: &'a Value,
    turn_id: &str,
    projection: &Map<String, Value>,
) -> Result<&'a Map<String, Value>, &'a str> {
    if status != "completed" {
        return Err("not_completed");
    }
    let Some(candidate) = candidate.as_object() else {
        return Err("unavailable");
    };
    let Some(value) = candidate.get("projection") else {
        return Err(match candidate.get("status").and_then(Value::as_str) {
            Some("stored") => "unavailable",
            Some(reason) if reason.chars().count() <= 64 => reason,
            _ => "invalid",
        });
    };
    let valid = value.as_object().is_some_and(|value| {
        let messages_valid =
            value
                .get("messages")
                .and_then(Value::as_array)
                .is_some_and(|messages| {
                    !messages.is_empty()
                        && messages.len() <= 16_384
                        && messages.iter().all(Value::is_object)
                        && messages
                            .last()
                            .and_then(|message| message.get("role"))
                            .and_then(Value::as_str)
                            == Some("assistant")
                });
        let turns_valid = value
            .get("turns")
            .and_then(Value::as_array)
            .is_some_and(|turns| {
                !turns.is_empty()
                    && turns.len() <= 4_096
                    && turns.iter().all(|witness| {
                        witness.as_array().is_some_and(|witness| {
                            witness.len() == 2
                                && witness[0]
                                    .as_str()
                                    .is_some_and(|id| !id.is_empty() && id.chars().count() <= 256)
                                && witness[1].as_str().is_some_and(|digest| {
                                    digest.len() == 64
                                        && digest.bytes().all(|byte| {
                                            byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)
                                        })
                                })
                        })
                    })
            });
        messages_valid && turns_valid
    });
    struct EncodingBudget(usize);
    impl io::Write for EncodingBudget {
        fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
            if bytes.len() > self.0 {
                return Err(io::Error::other("continuation exceeds encoding budget"));
            }
            self.0 -= bytes.len();
            Ok(bytes.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }
    if !valid || serde_json::to_writer(EncodingBudget(8 * 1024 * 1024), value).is_err() {
        return Err("invalid_or_over_budget");
    }
    // `valid` above proves the shape (non-empty turns of 2-element witness
    // arrays inside one object); keep the lookups non-panicking so a future
    // validation edit fails closed instead of unwinding the commit path.
    let Some(witness) = value["turns"].as_array().and_then(|turns| turns.last()) else {
        return Err("invalid_or_over_budget");
    };
    if witness.get(0).and_then(Value::as_str) != Some(turn_id)
        || candidate.get("content").unwrap_or(&Value::Null)
            != projection.get("content").unwrap_or(&Value::Null)
    {
        return Err("authority_changed");
    }
    value.as_object().ok_or("invalid_or_over_budget")
}

// Compact storage must never hide a live projection which cannot be settled.
// Count the materialized envelope, not the _projectionHead descriptor. Reserve
// one admitted terminal request plus bounded merged trace and scalar growth;
// unusually large restart anchors are measured rather than assumed small.
fn validate_settleable_turn_size(
    turn: &Map<String, Value>,
    projection: &Map<String, Value>,
    projection_bytes: usize,
    attempt: &Map<String, Value>,
    terminal: bool,
) -> io::Result<()> {
    let envelope: std::collections::BTreeMap<&str, &Value> = turn.iter()
        .filter(|(key, _)| key.as_str() != "projection" && key.as_str() != "_projectionHead")
        .map(|(key, value)| (key.as_str(), value))
        .collect();
    let envelope_bytes = serde_json::to_vec(&envelope)
        .map_err(|_| invalid_data("Turn envelope cannot be encoded"))?.len();
    let materialized_bytes = envelope_bytes.saturating_add(b",\"projection\":".len())
        .saturating_add(projection_bytes);
    let limit = crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES;
    let mut reserve = 0;
    if !terminal {
        reserve = MAX_LEGACY_DOCUMENT_BYTES + MAX_TIMING_TRACE_PERSISTED_BYTES + 1024;
        // Avoid rescanning the tool history on normal small streaming revisions.
        // A restart anchor can duplicate part of that history but not more than
        // the projection plus its bounded envelope. Inspect it near the ceiling.
        if materialized_bytes.saturating_add(projection_bytes).saturating_add(reserve) > limit {
            let recovery_bytes = serde_json::to_vec(&restart_settlement(attempt, projection))
                .map_err(|_| invalid_data("Turn recovery settlement cannot be encoded"))?.len();
            reserve = reserve.max(recovery_bytes.saturating_add(1024));
        }
    }
    if materialized_bytes.saturating_add(reserve) > limit {
        return Err(io::Error::new(io::ErrorKind::OutOfMemory,
            "Turn reconstructed state exceeds its settleable snapshot budget"));
    }
    Ok(())
}

/// Reuse the byte witness of the frozen projection. Only legacy slim frames
/// without a text-head witness use the historical two-field evidence encoding.
fn event_projection_evidence_bytes(
    request: &EventRecordRequest,
    text_projection_bytes: Option<usize>,
    counted_projection_bytes: usize,
) -> io::Result<usize> {
    if let Some(bytes) = text_projection_bytes {
        return Ok(bytes);
    }
    if request.slim && !request.terminal && request.projection_patch.is_none() {
        return serde_json::to_vec(&json!({
            "content": request.content,
            "thinking": request.thinking,
        })).map(|bytes| bytes.len())
            .map_err(|_| invalid_data("Turn projection cannot be encoded"));
    }
    Ok(counted_projection_bytes)
}

/// Resolve the owner/current-attempt fence through the same transaction reads.
/// Execution frames reuse this boundary only while their attempt stays running.
fn load_event_record_state(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest,
    materialize_projection: bool,
) -> io::Result<Option<(LoadedAttemptForUpdate, LoadedTurnForAttempt)>> {
    let Some(loaded_attempt) = load_attempt_for_update(database, transaction, &request.attempt_id)?
    else {
        return Ok(None);
    };
    let attempt_status = loaded_attempt
        .document
        .get("status")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt status is malformed"))?;
    if !matches!(attempt_status, "pending" | "running") {
        return Ok(None);
    }
    // Flow-root fence, mirroring the SQL sidecar's `flow_root_attempt_id`
    // check: the declared root is only a hint — the immutable attempt config
    // must prove it, and a declared root must still be a live current
    // attempt in the same conversation. Ordinary chat declares no root and
    // pays no parent read.
    let attempt_flow_root = loaded_attempt
        .document
        .get("_config")
        .and_then(Value::as_object)
        .and_then(|config| config.get("flowRootAttemptId"))
        .and_then(Value::as_str)
        .unwrap_or("");
    if attempt_flow_root != request.flow_root_attempt_id {
        return Ok(None);
    }
    if !attempt_flow_root.is_empty() {
        let parent_current_and_live =
            match load_attempt_for_update(database, transaction, attempt_flow_root)? {
                Some(parent) => {
                    attempt_is_live(&parent.document)
                        && parent.conversation_id == loaded_attempt.conversation_id
                        && load_current_turn_for_attempt(
                            database,
                            transaction,
                            attempt_flow_root,
                            &parent.document,
                            &parent.conversation_id,
                        )?
                        .is_some()
                }
                None => false,
            };
        if !parent_current_and_live {
            return Ok(None);
        }
    }
    let load_turn = if materialize_projection {
        load_current_turn_for_attempt
    } else { load_current_turn_header_for_attempt };
    let Some(loaded_turn) = load_turn(
        database,
        transaction,
        &request.attempt_id,
        &loaded_attempt.document,
        &loaded_attempt.conversation_id,
    )?
    else {
        return Ok(None);
    };
    let stored_task_id = loaded_attempt
        .document
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt task identity is malformed"))?;
    let starts_attempt = !request.terminal && attempt_status == "pending";
    if starts_attempt && (stored_task_id.is_empty() || stored_task_id != request.task_id) {
        return Ok(None);
    }

    Ok(Some((loaded_attempt, loaded_turn)))
}

pub(crate) fn event_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest,
) -> io::Result<Vec<u8>> {
    event_record_mode(database, transaction, request, true)
}

#[cfg(test)]
fn event_record_unframed(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest) -> io::Result<Vec<u8>>
{
    event_record_mode(database, transaction, request, false)
}

fn event_record_mode(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest, allow_frame_witness: bool) -> io::Result<Vec<u8>>
{
    if request.slim && request.projection_patch.is_some() {
        return Err(invalid_input("Slim Turn events cannot carry a projection patch"));
    }
    let text_append = text_tail::classify(request);
    let frame_candidate = allow_frame_witness && text_append.is_none()
        && execution_frame::classify_patch(request);
    let _load_stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TURN_EVENT_LOAD);
    let Some((loaded_attempt, loaded_turn)) = load_event_record_state(
        database, transaction, request, text_append.is_none() && !frame_candidate,
    )? else {
        return encode_response(&json!({"applied": false}));
    };
    let attempt_status = loaded_attempt.document.get("status").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt status is malformed"))?;
    let stored_task_id = loaded_attempt.document.get("taskId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt task identity is malformed"))?;
    let starts_attempt = !request.terminal && attempt_status == "pending";

    let mut turn = loaded_turn.document.clone();
    let mut verified_stable_segments = loaded_turn.verified_stable_segments.clone();
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
    let text_update = match &text_append {
        Some(append) => text_tail::append(database, transaction, &loaded_turn, append,
            request, &loaded_attempt.conversation_id, &turn_id)?,
        None => None,
    };
    let frame_update = if frame_candidate && !starts_attempt {
        execution_frame::append_from_witness(database, transaction, &loaded_turn,
            request, &loaded_attempt.conversation_id, &turn_id)?
    } else { None };
    if (text_append.is_some() || frame_candidate) && text_update.is_none() && frame_update.is_none() {
        if let Some(head) = &loaded_turn.projection_head {
            verified_stable_segments = materialize_projection_head(database, transaction, &mut turn, head)?;
        }
    }
    let previous_projection = turn
        .get("projection")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default();

    drop(_load_stage);
    let _projection_stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TURN_EVENT_PROJECTION);
    let (mut next_projection, mut replay_patch) = if text_update.is_some() || frame_update.is_some() {
        (Map::new(), request.projection_patch.clone()
            .ok_or_else(|| invalid_input("Turn text append requires a patch"))?)
    } else if request.slim && request.projection_patch.is_none() {
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
        // The producer explicitly attests that its patch targets the stable
        // segment view. Normalize under the revision lock, but publish a
        // replay patch from the exact committed wire document. Otherwise the
        // first segments[0] append cannot resolve, or reconnecting clients
        // miss the normalization repair.
        let patch_base = if request.projection_segments_stable {
            let (stable, proof) = execution_frame::normalize_for_patch(
                &previous_projection, previous_revision,
                turn.get("actor").and_then(Value::as_str).unwrap_or("assistant"),
                turn.get("status").and_then(Value::as_str).unwrap_or("running"),
                verified_stable_segments.as_ref(),
            )?;
            verified_stable_segments = proof;
            stable
        } else {
            previous_projection.clone()
        };
        let next = crate::turn_projection_patch::apply_projection_patch(Some(&patch_base), patch)
            .map_err(|_| invalid_input("Turn projection patch is invalid"))?;
        // normalize_for_patch already compared these exact old trees; reuse
        // that result as well as its no-op proof instead of walking them twice.
        let replay = if !request.projection_segments_stable || verified_stable_segments.is_some() {
            patch.clone()
        } else {
            crate::turn_projection_patch::build_projection_patch(
                &previous_projection,
                &next,
                previous_revision,
                next_revision,
            )
            .map_err(|_| {
                io::Error::new(
                    io::ErrorKind::OutOfMemory,
                    "Turn repair patch exceeds its bound",
                )
            })?
        };
        (next, replay)
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

    if preserve_guidance_answers(&previous_projection, &mut next_projection) {
        replay_patch = crate::turn_projection_patch::build_projection_patch(
            &previous_projection, &next_projection, previous_revision, next_revision,
        ).map_err(|_| invalid_data("Guidance preservation patch exceeds its bound"))?;
    }

    drop(_projection_stage);
    let _store_stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TURN_EVENT_STORE);
    let mut attempt = loaded_attempt.document.clone();
    // Measure an eligible materialized head once. The same actual field bytes
    // feed recovery, size admission and its published witness. Inline Turns
    // retain their original path until the physical owner promotes a head.
    let measured_frame = frame_candidate && text_update.is_none() && frame_update.is_none()
        && loaded_turn.projection_head.is_some();
    let frame_witness = if measured_frame {
        execution_frame::ProjectionWitness::mint_after_patch(&next_projection, next_revision,
            turn.get("actor").and_then(Value::as_str).unwrap_or("assistant"), &request.status,
            verified_stable_segments.as_ref(), &replay_patch)?
    } else { None };
    let mut projection_bytes = 0;
    if !request.terminal {
        let created_at_ms = attempt
            .get("createdAt")
            .and_then(Value::as_u64)
            .ok_or_else(|| invalid_data("attempt recovery timestamp is malformed"))?;
        projection_bytes = match text_update.as_ref().map(|u| u.projection_bytes)
            .or_else(|| frame_update.as_ref().map(|u| u.projection_bytes))
            .or_else(|| frame_witness.as_ref().map(|witness| witness.bytes())) {
            Some(bytes) => bytes,
            None => serde_json::to_vec(&next_projection)
                .map_err(|_| invalid_data("turn projection cannot be encoded"))?.len(),
        };
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
        if request.status == "superseded" {
            attempt.insert("supersededAt".to_owned(), Value::from(request.now_ms));
        }
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
    if request.terminal && request.status == "superseded" {
        database.entity_put(
            transaction,
            attempt_superseded_index_key(
                transaction,
                request.now_ms,
                &loaded_attempt.conversation_id,
                &request.attempt_id,
            )?,
            Vec::new(),
        )?;
    }

    // All terminal trace mutations are complete. Freeze this private tree so
    // its serialized-byte witness remains valid through storage/publication.
    let next_projection = next_projection;
    turn.insert("projectionRevision".to_owned(), Value::from(next_revision));
    turn.insert("status".to_owned(), Value::String(request.status.clone()));
    turn.insert("settlement".to_owned(), Value::Object(request.settlement.clone()));
    turn.insert("updatedAt".to_owned(), Value::from(request.now_ms));
    if request.terminal {
        projection_bytes = serde_json::to_vec(&next_projection)
            .map_err(|_| invalid_data("Turn projection cannot be encoded"))?.len();
    }
    validate_settleable_turn_size(
        &turn, &next_projection, projection_bytes, &attempt, request.terminal,
    )?;

    let mut next_projection_head = if let Some(update) = &text_update {
        Some(update.head.clone())
    } else if let Some(update) = &frame_update {
        Some(update.head.clone())
    } else {
        store_event_projection(database, transaction, request, &loaded_attempt.conversation_id,
            &turn_id, previous_revision, next_revision, loaded_turn.projection_head.as_ref(),
            &previous_projection, &next_projection, projection_bytes, &replay_patch)?
    };
    if frame_candidate && text_update.is_none() && frame_update.is_none() {
        if let Some(head) = &mut next_projection_head {
            head.frame_witness = if measured_frame { frame_witness } else {
                execution_frame::ProjectionWitness::mint_after_patch(&next_projection,
                    next_revision, turn.get("actor").and_then(Value::as_str).unwrap_or("assistant"),
                    &request.status, verified_stable_segments.as_ref(), &replay_patch)?
            };
        }
    }
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
    crate::versioned_document::put_with_blob_owner_bounded(
        database,
        transaction,
        crate::versioned_document::PutRequest {
            key: loaded_turn.key.clone(),
            namespace: DOCUMENT_IDENTITY.to_owned(),
            logical_key: turn_logical_key(&loaded_attempt.conversation_id, &turn_id),
            value_json: serde_json::to_vec(&turn)
                .map_err(|_| invalid_data("turn cannot be encoded"))?,
            expected_version: Some(loaded_turn.physical_version),
            updated_at_ms: request.now_ms,
        },
        transaction.owner_user_id(),
        crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let staged = database
        .entity_get(transaction, &loaded_turn.key)?
        .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
    store_changed_lane_turn(database, transaction, &loaded_attempt.conversation_id, &lane_id, ordinal, staged)?;
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

    drop(_store_stage);
    let _publish_stage = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TURN_EVENT_PUBLISH);
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
    let projection_bytes = event_projection_evidence_bytes(
        request,
        text_update.as_ref().map(|update| update.projection_bytes),
        projection_bytes,
    )?;
    publish_event_record(database, transaction, request, EventPublication {
        conversation_id: &loaded_attempt.conversation_id, turn: &turn,
        attempt: &attempt, projection: &next_projection, projection_bytes,
        replay_patch, starts_attempt,
    })
}

// One physical checkpoint/patch-chain policy for ordinary events and frames.
// The caller supplies the exact materialized before/after states when needed;
// unchanged bounded head chains append their original replay patch only.
fn store_event_projection(database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest, conversation_id: &str, turn_id: &str,
    previous_revision: u64, next_revision: u64, current_head: Option<&ProjectionHeadDescriptor>,
    previous_projection: &Map<String, Value>, next_projection: &Map<String, Value>,
    next_projection_bytes: usize, replay_patch: &Map<String, Value>,
) -> io::Result<Option<ProjectionHeadDescriptor>> {
    let replay_patch_bytes = serde_json::to_vec(replay_patch)
        .map_err(|_| invalid_data("Turn projection patch cannot be encoded"))?.len();
    Ok(if request.terminal {
        retire_event_projection_head(
            database,
            transaction,
            conversation_id,
            turn_id,
            current_head,
        )?;
        None
    } else if let Some(current) = current_head {
        let next_count = current
            .patch_count
            .checked_add(1)
            .ok_or_else(|| invalid_data("Turn projection patch count overflows"))?;
        let next_bytes = current
            .patch_bytes
            .checked_add(replay_patch_bytes)
            .ok_or_else(|| invalid_data("Turn projection patch bytes overflow"))?;
        if current.text_tail.is_none() && next_count <= MAX_TURN_PROJECTION_HEAD_PATCHES
            && next_bytes <= MAX_TURN_PROJECTION_PATCH_BYTES
        {
            let next_head = ProjectionHeadDescriptor {
                head_id: current.head_id.clone(),
                attempt_id: current.attempt_id.clone(),
                base_revision: current.base_revision,
                patch_count: next_count,
                patch_bytes: next_bytes,
                text_tail: None, frame_witness: None,
            };
            store_projection_head_patch(
                database,
                transaction,
                conversation_id,
                turn_id,
                &next_head,
                &replay_patch,
                request.now_ms,
            )?;
            Some(next_head)
        } else {
            retire_event_projection_head(
                database,
                transaction,
                conversation_id,
                turn_id,
                Some(current),
            )?;
            let checkpoint_head = ProjectionHeadDescriptor {
                head_id: projection_head_id(&request.attempt_id, next_revision),
                attempt_id: request.attempt_id.clone(),
                base_revision: next_revision,
                patch_count: 0,
                patch_bytes: 0,
                text_tail: None, frame_witness: None,
            };
            store_projection_checkpoint(
                database,
                transaction,
                conversation_id,
                turn_id,
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
        if previous_projection_bytes > MAX_TURN_PROJECTION_INLINE_LIVE_BYTES
            || next_projection_bytes > MAX_TURN_PROJECTION_INLINE_LIVE_BYTES
        {
            // A wire patch may exceed the compact-head chain budget. Fold
            // directly into a checkpoint at the new revision in that case;
            // never publish a head its reader would reject on the next read.
            let fold = replay_patch_bytes > MAX_TURN_PROJECTION_PATCH_BYTES;
            let base_revision = if fold {
                next_revision
            } else {
                previous_revision
            };
            let head = ProjectionHeadDescriptor {
                head_id: projection_head_id(&request.attempt_id, base_revision),
                attempt_id: request.attempt_id.clone(),
                base_revision,
                patch_count: if fold { 0 } else { 1 },
                patch_bytes: if fold { 0 } else { replay_patch_bytes },
                text_tail: None, frame_witness: None,
            };
            store_projection_checkpoint(
                database,
                transaction,
                conversation_id,
                turn_id,
                &head,
                if fold {
                    &next_projection
                } else {
                    &previous_projection
                },
                request.now_ms,
            )?;
            if !fold {
                store_projection_head_patch(
                    database,
                    transaction,
                    conversation_id,
                    turn_id,
                    &head,
                    &replay_patch,
                    request.now_ms,
                )?;
            }
            Some(head)
        } else {
            None
        }
    })
}

struct EventPublication<'a> {
    conversation_id: &'a str,
    turn: &'a Map<String, Value>,
    attempt: &'a Map<String, Value>,
    projection: &'a Map<String, Value>,
    projection_bytes: usize,
    replay_patch: Map<String, Value>,
    starts_attempt: bool,
}

fn publish_event_record(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &EventRecordRequest,
    publication: EventPublication<'_>,
) -> io::Result<Vec<u8>> {
    let turn_id = publication.turn.get("turnId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("turn identity is malformed"))?;
    let ordinal = publication.turn.get("ordinal").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn ordinal is malformed"))?;
    let next_revision = publication.turn.get("projectionRevision").and_then(Value::as_u64)
        .ok_or_else(|| invalid_data("turn projection revision is malformed"))?;
    let mut event_payload = request.event_payload.clone();
    event_payload.remove("removedQueueIds");
    if let Some(acknowledgement) = &request.background_command_ack {
        if request.slim {
            return Err(invalid_input(
                "background acknowledgement requires full receipt",
            ));
        }
        let removed_ids = crate::queue::acknowledge_background_receipts(
            database,
            transaction,
            publication.conversation_id,
            acknowledgement,
            &publication.projection,
        )?;
        event_payload.insert("removedQueueIds".to_owned(), json!(removed_ids));
    }

    let continuation_status = if request.terminal {
        Some(
            match terminal_continuation_candidate(
                &request.status,
                &request.model_continuation,
                &turn_id,
                &publication.projection,
            ) {
                Ok(candidate_projection) => {
                    let mut settled_turn = publication.turn.clone();
                    settled_turn.insert(
                        "projection".to_owned(),
                        Value::Object(publication.projection.clone()),
                    );
                    crate::model_continuation::settle(
                        database,
                        transaction,
                        publication.conversation_id,
                        ordinal,
                        &settled_turn,
                        candidate_projection,
                        request.now_ms,
                    )?
                }
                Err(reason) => reason,
            },
        )
    } else {
        None
    };
    if request.terminal {
        event_payload.insert(
            "modelContinuationStatus".to_owned(),
            json!(continuation_status),
        );
    }
    event_payload.remove("projection");
    event_payload.insert("projectionPatch".to_owned(), Value::Object(publication.replay_patch));
    event_payload.insert("status".to_owned(), Value::String(request.status.clone()));
    event_payload.insert("projectionBytes".to_owned(), Value::from(publication.projection_bytes));
    if publication.starts_attempt {
        event_payload.insert(
            "attempts".to_owned(),
            Value::Array(vec![public_attempt(publication.attempt)?]),
        );
    }
    append_attempt_event(
        database,
        transaction,
        AttemptEventAppend {
            conversation_id: publication.conversation_id,
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
    for item in &request.preceding_task_events {
        crate::indexed_stream::append(database, transaction, &crate::indexed_stream::AppendRequest {
            task_id: item.task_id.clone(), application_sequence: item.application_sequence,
            event_type: item.event_type.clone(), payload_json: item.payload_json.clone(),
            created_at_ms: item.created_at_ms,
        })?;
    }
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
        "task_event": task_event,
        "task_event_count": request.preceding_task_events.len() + usize::from(request.task_event.is_some()),
        "model_continuation_status": continuation_status
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
        let mut projection = loaded_turn
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

        effect_interruption::record_interrupted_file_effects(&mut projection,
            text("conversationId")?, text("turnId")?, &attempt_id,
            loaded_attempt.document.get("taskId").and_then(Value::as_str).unwrap_or(""));
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
        // Recovery folds the projection head into a snapshot, which can exceed
        // the request-literal bound after patch growth. Match the Turn reader's bound.
        crate::versioned_document::put_with_blob_owner_bounded(
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
            transaction.owner_user_id(),
            crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
        )?;
        let staged = database
            .entity_get(transaction, &loaded_turn.key)?
            .ok_or_else(|| invalid_data("staged turn document disappeared"))?;
        store_changed_lane_turn(database, transaction, &conversation_id, &lane_id, ordinal, staged)?;
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
    deadline_elapsed: &mut dyn FnMut() -> bool,
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
    'pages: while events.len() < limit {
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
            // A full replay can materialize thousands of event documents,
            // each a payload read behind the single authority mutex. Return
            // the contiguous prefix once the caller's deadline is spent so a
            // network-mounted authority is never monopolized past it; with
            // zero progress the honest answer is `deadline_elapsed`, never a
            // silent empty page (which a caller must not read as end-of-list).
            if deadline_elapsed() {
                if events.is_empty() {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "attempt event list deadline elapsed",
                    ));
                }
                break 'pages;
            }
            let key_bytes = key.key_bytes();
            if key_bytes.len() != prefix.len() + 8 || !key_bytes.starts_with(&prefix) {
                return Err(invalid_data("attempt event key identity is malformed"));
            }
            let sequence = u64::from_be_bytes(
                key_bytes[prefix.len()..]
                    .try_into()
                    .expect("attempt event sequence is eight bytes"),
            );
            let (_, document_json) =
                crate::versioned_document::materialize_stored_document_bounded(
                    database,
                    transaction.tenant_id(),
                    transaction.owner_user_id(),
                    &stored,
                    ATTEMPT_EVENT_DOCUMENT_IDENTITY,
                    MAX_ATTEMPT_EVENT_DOCUMENT_BYTES,
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
            let next_bytes = response_bytes
                .checked_add(document_json.len())
                .and_then(|bytes| bytes.checked_add(usize::from(!events.is_empty())))
                .ok_or_else(|| invalid_data("attempt event page byte count overflow"))?;
            if next_bytes > MAX_ATTEMPT_EVENT_PAGE_BYTES {
                break 'pages;
            }
            response_bytes = next_bytes;
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
    let encoded = serde_json::to_vec(&Value::Array(events))?;
    if encoded.len() > MAX_ATTEMPT_EVENT_PAGE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::OutOfMemory,
            "hydrated event response exceeds its page budget",
        ));
    }
    Ok(Some(encoded))
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
    if let Some(outcome) = event_retirement::sweep(database, transaction, request)? {
        return Ok(outcome);
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
        let loaded = match load_attempt_for_update(database, transaction, &attempt_id)? {
            Some(loaded) => loaded,
            // A retention/GC sweep must never poison the authority on an
            // orphaned index entry (its attempt is already gone): drop just
            // the stale locator and keep sweeping — the same terminal case as
            // `head == 0` below, not a data-integrity violation.
            None => {
                database.entity_delete(transaction, index_key.clone())?;
                completed_candidates += 1;
                continue;
            }
        };
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
        let (_, turn_json) = crate::versioned_document::materialize_stored_document_bounded(
            database,
            transaction.tenant_id(),
            transaction.owner_user_id(),
            &stored_turn,
            DOCUMENT_IDENTITY,
            crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
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
        let (_, event_json) = sync_attempt_reference::materialize(
            database, transaction, &event_key, &stored,
            MAX_LEGACY_DOCUMENT_BYTES, false)?;
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

enum CleanupCandidate {
    Delete {
        loaded: LoadedAttemptForUpdate,
        turn_id: String,
        directory_position: usize,
        entries: Vec<Map<String, Value>>,
    },
    Skip,
    Stale,
}

/// Validates one superseded-index entry against current authority. Anything
/// that no longer proves the Attempt is superseded, unreferenced, and intact
/// fails closed: the entry is either pruned as stale (the Attempt or its
/// supersession evidence is gone) or retained while the candidate is skipped.
fn cleanup_candidate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    attempt_id: &str,
    superseded_at_ms: u64,
) -> io::Result<CleanupCandidate> {
    let Some(loaded) = load_attempt_for_update(database, transaction, attempt_id)? else {
        return Ok(CleanupCandidate::Stale);
    };
    if loaded.conversation_id != conversation_id {
        return Ok(CleanupCandidate::Stale);
    }
    let document = &loaded.document;
    if document.get("status").and_then(Value::as_str) != Some("superseded")
        || document.get("supersededAt").and_then(Value::as_u64) != Some(superseded_at_ms)
    {
        return Ok(CleanupCandidate::Stale);
    }
    let turn_id = document
        .get("turnId")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid_data("attempt turn identity is malformed"))?
        .to_owned();
    // A live Turn whose current attempt is this Attempt still references it;
    // a missing Turn is an integrity anomaly because Turn deletion removes
    // every owned Attempt. Neither may delete anything.
    let Some(stored_turn) = database.entity_get(
        transaction,
        &turn_key(transaction, conversation_id, &turn_id)?,
    )?
    else {
        return Ok(CleanupCandidate::Skip);
    };
    let turn = materialize_turn(database, transaction, &stored_turn)?;
    if turn.get("currentAttemptId").and_then(Value::as_str) == Some(attempt_id) {
        return Ok(CleanupCandidate::Skip);
    }
    // Retained conversation-sync replay data may still reference the Attempt;
    // only turn.sync.prune may release those fences.
    let reference_prefix = attempt_sync_reference_prefix(attempt_id)?;
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
        return Ok(CleanupCandidate::Skip);
    }
    let entries = load_attempt_turn_directory(
        database,
        transaction,
        conversation_id,
        &turn_id,
        Some(attempt_id),
    )?;
    let Some(directory_position) = entries
        .iter()
        .position(|entry| directory_entry_text(entry, "attemptId").ok() == Some(attempt_id))
    else {
        return Ok(CleanupCandidate::Skip);
    };
    Ok(CleanupCandidate::Delete {
        loaded,
        turn_id,
        directory_position,
        entries,
    })
}

/// Deletes one validated superseded Attempt with its derived indexes, event
/// range, and exact Turn directory/count repair. The tenant-global Attempt
/// identity claim and creation-command idempotency records are retained so a
/// later create cannot ABA a deleted identity.
#[allow(clippy::too_many_arguments)]
fn delete_superseded_attempt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    attempt_id: &str,
    loaded: LoadedAttemptForUpdate,
    turn_id: &str,
    directory_position: usize,
    mut entries: Vec<Map<String, Value>>,
) -> io::Result<()> {
    let entry = entries.remove(directory_position);
    remove_directory_entry_indexes(database, transaction, conversation_id, &entry)?;
    store_attempt_turn_directory(database, transaction, conversation_id, turn_id, &entries)?;
    let count_key = attempt_turn_count_key(transaction, conversation_id, turn_id)?;
    let count = decode_u64(
        database.entity_get(transaction, &count_key)?,
        "turn attempt count is malformed",
    )?;
    let remaining = count
        .checked_sub(1)
        .ok_or_else(|| invalid_data("turn attempt count underflow"))?;
    if remaining == 0 {
        database.entity_delete(transaction, count_key)?;
    } else {
        database.entity_put(transaction, count_key, remaining.to_le_bytes().to_vec())?;
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
    crate::versioned_document::delete(
        database,
        transaction,
        loaded.key,
        "generation_attempts",
        attempt_id,
        Some(loaded.physical_version),
    )?;
    Ok(())
}

/// Bounded owner-scoped superseded-Attempt cleanup. The persistent
/// age-leading superseded index is scanned oldest-first; every candidate is
/// revalidated against the live Attempt, its owning Turn, and retained replay
/// fences before anything is written, and the whole round commits as one
/// authority transaction.
pub(crate) fn cleanup(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &CleanupRequest,
) -> io::Result<Vec<u8>> {
    let cutoff_ms = request.now_ms.saturating_sub(request.retention_ms);
    let (start, end) = EntityKey::prefix_range(
        transaction.tenant_id(),
        transaction.owner_user_id(),
        ATTEMPT_SUPERSEDED_INDEX_NAMESPACE,
        &[],
    )?;
    let rows = database.entity_scan(
        transaction,
        &start,
        &end,
        MAX_CLEANUP_INDEX_SCAN_ROWS_PER_TRANSACTION,
    )?;
    let mut scanned = 0_usize;
    let mut deleted_attempt_ids = Vec::new();
    let mut skipped = 0_usize;
    let mut pruned = 0_usize;
    let mut removed_archive_bytes = 0_u64;
    let mut limited = false;
    for (key, _) in &rows {
        let (superseded_at_ms, conversation_id, attempt_id) = decode_superseded_index_key(key)?;
        if superseded_at_ms > cutoff_ms {
            break;
        }
        scanned += 1;
        if deleted_attempt_ids.len() >= request.limit {
            limited = true;
            break;
        }
        match cleanup_candidate(
            database,
            transaction,
            &conversation_id,
            &attempt_id,
            superseded_at_ms,
        )? {
            CleanupCandidate::Stale => {
                database.entity_delete(transaction, key.clone())?;
                pruned += 1;
            }
            CleanupCandidate::Skip => {
                skipped += 1;
            }
            CleanupCandidate::Delete {
                loaded,
                turn_id,
                directory_position,
                entries,
            } => {
                // Raw archives are removed with a strict row budget; an
                // over-budget Attempt is left untouched for the next round.
                let Some(archive_bytes) = crate::raw_archive::delete_attempt(
                    database,
                    transaction,
                    &conversation_id,
                    &attempt_id,
                    MAX_CLEANUP_INDEX_SCAN_ROWS_PER_TRANSACTION,
                )?
                else {
                    skipped += 1;
                    continue;
                };
                delete_superseded_attempt(
                    database,
                    transaction,
                    &conversation_id,
                    &attempt_id,
                    loaded,
                    &turn_id,
                    directory_position,
                    entries,
                )?;
                database.entity_delete(transaction, key.clone())?;
                removed_archive_bytes = removed_archive_bytes
                    .checked_add(archive_bytes)
                    .ok_or_else(|| invalid_data("cleanup archive byte total overflow"))?;
                deleted_attempt_ids.push(attempt_id);
            }
        }
    }
    let deleted = deleted_attempt_ids.len();
    encode_response(&json!({
        "deletedAttempts": deleted,
        "deletedAttemptIds": deleted_attempt_ids,
        "skippedAttempts": skipped,
        "prunedIndexEntries": pruned,
        "scannedIndexEntries": scanned,
        "removedRawArchiveBytes": removed_archive_bytes,
        "moreCandidates": limited || rows.len() == MAX_CLEANUP_INDEX_SCAN_ROWS_PER_TRANSACTION,
        "backend": "tofu-db",
    }))
}

/// Sort keys lifted from one stored attempt document without materializing
/// its full `Value` tree: the response embeds the stored bytes verbatim
/// (canonical JSON round-trips byte-identically through `RawValue`), so the
/// full parse would only serve the (createdAt, attemptId) ordering below.
/// `RawValue::from_string` re-validates the document, so malformed stored
/// bytes fail closed exactly like the previous full `Value` parse.
#[derive(serde::Deserialize)]
struct AttemptSortKey<'a> {
    #[serde(rename = "createdAt", default)]
    created_at: u64,
    #[serde(rename = "attemptId", default)]
    attempt_id: &'a str,
}

fn attempts_for_turns(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    turns: &[Value],
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Vec<Box<RawValue>>> {
    let _profile = crate::request_profile::Probe::new(crate::request_profile::TURN_SNAPSHOT_ATTEMPTS);
    let mut attempts: Vec<(u64, String, Box<RawValue>)> = Vec::new();
    for turn in turns {
        // Attempts must stay complete for the turns a snapshot/page already
        // returned — a silently partial attempt map would corrupt the client
        // view, so exhaustion here fails the operation instead of truncating.
        if deadline_elapsed() {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "turn attempt materialization deadline elapsed",
            ));
        }
        let Some(attempt_id) = turn.get("currentAttemptId").and_then(Value::as_str) else {
            continue;
        };
        let encoded = attempt_get(database, transaction, attempt_id)?
            .ok_or_else(|| invalid_data("turn current attempt is missing"))?;
        let sort_key: AttemptSortKey = serde_json::from_slice(&encoded)
            .map_err(|_| invalid_data("attempt response is malformed"))?;
        let (created_at, attempt_id) = (sort_key.created_at, sort_key.attempt_id.to_owned());
        let raw = RawValue::from_string(
            String::from_utf8(encoded)
                .map_err(|_| invalid_data("attempt response is malformed"))?,
        )
        .map_err(|_| invalid_data("attempt response is malformed"))?;
        attempts.push((created_at, attempt_id, raw));
    }
    attempts.sort_by(|left, right| (left.0, left.1.as_str()).cmp(&(right.0, right.1.as_str())));
    Ok(attempts.into_iter().map(|(_, _, raw)| raw).collect())
}

fn lane_total(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
) -> io::Result<usize> {
    let lane_state = read_lane_state(database, transaction, conversation_id, lane_id)?;
    usize::try_from(lane_count_of(
        database,
        transaction,
        conversation_id,
        lane_id,
        lane_state.as_ref(),
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

/// Conservative serialized UTF-8 estimate without hydrating omitted blob bodies.
/// Inline documents already came from the index; external heads contribute only
/// checkpoint lengths, patch byte witnesses and the exact text-tail size.
fn tail_projection_estimate(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    stored: &[u8],
) -> io::Result<u64> {
    let bytes = crate::versioned_document::stored_document_logical_bytes(stored, DOCUMENT_IDENTITY)?;
    if crate::versioned_document::stored_blob_reference(stored)?.is_some() {
        return Ok(bytes);
    }
    let (_, value, _) = crate::versioned_document::materialize_stored_document_value_bounded(
        database, transaction.tenant_id(), transaction.owner_user_id(), stored,
        DOCUMENT_IDENTITY, crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES,
    )?;
    let document = value.as_object().ok_or_else(|| invalid_data("turn document is malformed"))?;
    let Some(head) = projection_head_from_document(document)? else { return Ok(bytes); };
    if let Some(tail) = &head.text_tail {
        return Ok(bytes.saturating_add(tail.projection_bytes as u64));
    }
    let conversation_id = document.get("conversationId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn head conversation is malformed"))?;
    let turn_id = document.get("turnId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn head identity is malformed"))?;
    let key = projection_checkpoint_key(transaction, conversation_id, turn_id, &head.head_id)?;
    let checkpoint = database.entity_get(transaction, &key)?
        .ok_or_else(|| invalid_data("Turn projection checkpoint is missing"))?;
    let checkpoint_bytes = crate::versioned_document::stored_document_logical_bytes(
        &checkpoint, PROJECTION_HEAD_DOCUMENT_IDENTITY,
    )?;
    Ok(bytes.saturating_add(checkpoint_bytes).saturating_add(head.patch_bytes as u64))
}

fn lane_tail_before(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    before_ordinal: u64,
    limit: usize,
    max_projection_bytes: usize,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<(Vec<Value>, bool)> {
    let _profile = crate::request_profile::Probe::new(crate::request_profile::TURN_SNAPSHOT_WINDOW);
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
    let mut has_more = rows.len() > limit;
    let execution_epoch =
        crate::conversation_header::execution_epoch(database, transaction, conversation_id)?;
    let mut turns = Vec::new();
    let mut projection_bytes = 0_u64;
    for (_, stored) in rows.into_iter().take(limit) {
        if max_projection_bytes != 0 {
            let estimate = tail_projection_estimate(database, transaction, &stored)?;
            if !turns.is_empty() && projection_bytes.saturating_add(estimate) > max_projection_bytes as u64 {
                has_more = true;
                break;
            }
            projection_bytes = projection_bytes.saturating_add(estimate);
        }
        // Every turn materialization is a payload read behind the single
        // authority mutex. On deadline exhaustion keep the newest turns
        // already read and mark the window resumable (`has_more` forces
        // `nextBeforeOrdinal` at the oldest retained turn); with zero turns
        // the honest answer is `deadline_elapsed`, never a silent tail page.
        if deadline_elapsed() {
            if turns.is_empty() {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "lane tail read deadline elapsed",
                ));
            }
            has_more = true;
            break;
        }
        turns.push(
            materialize_turn(database, transaction, &stored)
                .and_then(|turn| public_turn(turn, execution_epoch))?,
        );
    }
    turns.reverse();
    Ok((turns, has_more))
}

/// Private snapshot trees move into the canonical response exactly once. The
/// generic IR slot owns the resulting bytes and their validation evidence.
struct SyncSnapshotResponse {
    active_attempt_phase: Option<Value>,
    attempts: Value,
    conversation_id: String,
    conversation_revision: u64,
    has_artifacts: Option<bool>,
    queue_items: Vec<Value>,
    settings: Map<String, Value>,
    sync_sequence: u64,
    turn_window: Option<Value>,
    turns: Vec<Value>,
}

impl SyncSnapshotResponse {
    fn into_encoded(self) -> io::Result<crate::response_json::EncodedResponse> {
        let _profile = crate::request_profile::Probe::new(crate::request_profile::TURN_SNAPSHOT_ENCODE);
        let mut response = Map::new();
        if let Some(phase) = self.active_attempt_phase { response.insert("activeAttemptPhase".into(), phase); }
        response.insert("attempts".into(), self.attempts);
        response.insert("conversationId".into(), Value::String(self.conversation_id));
        response.insert("conversationRevision".into(), Value::from(self.conversation_revision));
        if let Some(hint) = self.has_artifacts { response.insert("hasArtifacts".into(), Value::Bool(hint)); }
        response.insert("queueItems".into(), Value::Array(self.queue_items));
        response.insert("settings".into(), Value::Object(self.settings));
        response.insert("syncSequence".into(), Value::from(self.sync_sequence));
        if let Some(window) = self.turn_window { response.insert("turnWindow".into(), window); }
        response.insert("turns".into(), Value::Array(self.turns));
        let encoded = crate::response_json::EncodedResponse::from_value(Value::Object(response))
            .map_err(|_| invalid_data("turn response cannot be encoded"))?;
        if encoded.len() > crate::generated_tofudb_ir::MAX_TURN_RESPONSE_BYTES {
            return Err(io::Error::new(io::ErrorKind::OutOfMemory,
                "turn response exceeds its bounded streamed payload"));
        }
        Ok(encoded)
    }
}

#[cfg(test)]
pub(crate) fn sync_snapshot(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_limit: usize,
    max_projection_bytes: usize,
    include_artifact_hint: bool,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<Vec<u8>>> {
    sync_snapshot_with_proof(database, transaction, conversation_id, turn_limit,
        max_projection_bytes, include_artifact_hint, deadline_elapsed)
        .map(|response| response.map(crate::response_json::EncodedResponse::into_bytes))
}

pub(crate) fn sync_snapshot_with_proof(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    turn_limit: usize,
    max_projection_bytes: usize,
    include_artifact_hint: bool,
    deadline_elapsed: &mut dyn FnMut() -> bool,
) -> io::Result<Option<crate::response_json::EncodedResponse>> {
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
            max_projection_bytes,
            deadline_elapsed,
        )?;
        turns = window;
        turn_window = Some(json!({
            "laneId": "main",
            "nextBeforeOrdinal": if has_more { turns.first().and_then(|turn| turn.get("ordinal")).cloned().unwrap_or(Value::Null) } else { Value::Null },
            "hasMore": has_more,
            "totalTurns": total
        }));
    } else {
        turns = list_values_until(
            database,
            transaction,
            conversation_id,
            None,
            deadline_elapsed,
        )?;
    }
    let attempts = json!(attempts_for_turns(database, transaction, &turns, deadline_elapsed)?);
    // A queued Turn pair is durable before its attempt starts, so its only
    // browser-visible explanation is the queue row. Omitting it leaves the
    // pending output Turn with no block at all; the snapshot must carry the
    // same owner-scoped projection `queue.list` serves, read in this same
    // transaction so the response stays a function of one entity snapshot.
    let queue_items = crate::queue::snapshot_items(
        database, transaction, conversation_id, deadline_elapsed)?;
    let active_attempt_phase = snapshot_phase::active_attempt_phase(
        database, transaction, conversation_id, &turns, &attempts)?;
    let sync_sequence = sync_head(database, transaction, conversation_id)?;
    let has_artifacts = if include_artifact_hint {
        crate::artifact::conversation_empty_hint(database, transaction, conversation_id)?
    } else { None };
    // Keep the previous RawValue-to-Value conversion for attempts: raw source
    // bytes may be noncanonical, and response serialization must normalize them.
    // Move the private trees into one canonical Value; json! on the complete
    // response would clone them. No parsed cache or escaped mutable view exists.
    SyncSnapshotResponse { active_attempt_phase, attempts,
        conversation_id: conversation_id.to_owned(), conversation_revision: revision,
        has_artifacts, queue_items,
        settings, sync_sequence, turn_window, turns }.into_encoded().map(Some)
}

/// Resolve the legacy compact head from its canonical owner-scoped document.
/// External projection payloads are deliberately never hydrated by navigation.
fn round_directory_actor<'a>(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, lane_id: &str, ordinal: u64, metadata: &'a Value,
) -> io::Result<std::borrow::Cow<'a, str>> {
    if let Some(actor) = metadata.get("actor") {
        return actor.as_str().map(std::borrow::Cow::Borrowed)
            .ok_or_else(|| invalid_data("Turn directory actor malformed"));
    }
    let turn_id = metadata.get("turnId").and_then(Value::as_str)
        .ok_or_else(|| invalid_data("Turn directory identity malformed"))?;
    let stored = database.entity_get(transaction,
        &turn_key(transaction, conversation_id, turn_id)?)?
        .ok_or_else(|| invalid_data("Turn directory legacy target missing"))?;
    let (_, value, _) = crate::versioned_document::materialize_stored_document_value_bounded(
        database, transaction.tenant_id(), transaction.owner_user_id(), &stored,
        DOCUMENT_IDENTITY, crate::generated_tofudb_ir::MAX_TURN_SNAPSHOT_BYTES)?;
    let document = value.as_object()
        .ok_or_else(|| invalid_data("Turn directory legacy target malformed"))?;
    if document.get("conversationId").and_then(Value::as_str) != Some(conversation_id)
        || document.get("turnId").and_then(Value::as_str) != Some(turn_id)
        || document.get("laneId").and_then(Value::as_str) != Some(lane_id)
        || document.get("ordinal").and_then(Value::as_u64) != Some(ordinal) {
        return Err(invalid_data("Turn directory legacy target identity differs"));
    }
    let mut expected: Value = serde_json::from_slice(&lane_compaction_index_value(document)?)
        .map_err(|_| invalid_data("Turn directory legacy head malformed"))?;
    let actor = expected.as_object_mut().unwrap().remove("actor").unwrap();
    if &expected != metadata {
        return Err(invalid_data("Turn directory legacy head differs from target"));
    }
    Ok(std::borrow::Cow::Owned(actor.as_str()
        .ok_or_else(|| invalid_data("Turn directory legacy actor malformed"))?.to_owned()))
}

/// Exact owner/lane input ranks, retaining bounded compact metadata only.
pub(crate) fn sync_round_directory(
    database: &AuthorityDatabase, transaction: &mut AuthorityTransaction,
    conversation_id: &str, lane_id: &str, offset: u64, limit: usize,
    expected: u64, anchor: &str, deadline: &mut dyn FnMut() -> bool,
) -> io::Result<Option<Vec<u8>>> {
    if crate::conversation_header::sync_header(database, transaction, conversation_id)?.is_none() { return Ok(None); }
    let head = sync_head(database, transaction, conversation_id)?;
    let mut result = json!({"ownerId": transaction.owner_user_id(), "conversationId": conversation_id, "laneId": lane_id,
        "syncSeq": head, "stale": head != expected, "totalRounds": 0,
        "offset": offset, "entries": []});
    if head == expected {
        let prefix = lane_prefix(conversation_id, lane_id)?;
        let (mut start, end) = EntityKey::prefix_range(transaction.tenant_id(),
            transaction.owner_user_id(), TURN_LANE_COMPACTION_INDEX_NAMESPACE, &prefix)?;
        let mut count = 0_u64;
        let mut seen = 0_u64;
        let mut entries = Vec::new();
        loop {
            if deadline() { return Err(io::Error::new(io::ErrorKind::TimedOut, "Turn directory deadline elapsed")); }
            let page = database.entity_scan(transaction, &start, &end, INDEX_PAGE_ROWS)?;
            if page.is_empty() { break; }
            start = after_key(&page.last().expect("nonempty directory page").0,
                transaction, TURN_LANE_COMPACTION_INDEX_NAMESPACE)?;
            for (key, encoded) in page {
                seen += 1;
                let metadata: Value = serde_json::from_slice(&encoded)
                    .map_err(|_| invalid_data("Turn directory head malformed"))?;
                let ordinal = u64::from_be_bytes(key.key_bytes().get(prefix.len()..prefix.len() + 8)
                    .filter(|_| key.key_bytes().len() == prefix.len() + 8)
                    .ok_or_else(|| invalid_data("Turn directory key malformed"))?
                    .try_into().unwrap());
                let actor = round_directory_actor(database, transaction,
                    conversation_id, lane_id, ordinal, &metadata)?;
                let turn_id = metadata.get("turnId").and_then(Value::as_str)
                    .ok_or_else(|| invalid_data("Turn directory identity malformed"))?;
                if matches!(actor.as_ref(), "human" | "critic" | "virtual_user") {
                    count += 1;
                    if count > offset && entries.len() < limit {
                        entries.push(json!({"turnId": turn_id,
                            "ordinal": ordinal,
                            "actor": actor, "roundNumber": count}));
                    }
                }
                if turn_id == anchor && count > 0 { result["activeRound"] = json!(count); }
            }
        }
        let lane_state = read_lane_state(database, transaction, conversation_id, lane_id)?;
        if seen != lane_count_of(database, transaction, conversation_id, lane_id, lane_state.as_ref())? {
            return Err(invalid_data("Turn directory compact head count differs"));
        }
        result["totalRounds"] = json!(count);
        if offset.saturating_add(entries.len() as u64) < count {
            result["nextOffset"] = json!(offset + entries.len() as u64);
        }
        result["entries"] = json!(entries);
    }
    serde_json::to_vec(&result).map(Some).map_err(|_| invalid_data("Turn directory encoding failed"))
}

pub(crate) fn sync_page(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
    lane_id: &str,
    before_ordinal: u64,
    limit: usize,
    expected_sync_sequence: u64,
    max_projection_bytes: usize,
    deadline_elapsed: &mut dyn FnMut() -> bool,
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
        max_projection_bytes,
        deadline_elapsed,
    )?;
    let next_before_ordinal = turns
        .first()
        .and_then(|turn| turn.get("ordinal"))
        .cloned()
        .unwrap_or(Value::Null);
    let attempts = attempts_for_turns(database, transaction, &turns, deadline_elapsed)?;
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
    deadline_elapsed: &mut dyn FnMut() -> bool,
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
    'pages: while events.len() < limit {
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
        for (key, stored) in rows {
            // Same mid-loop deadline contract as `events_list`: replaying a
            // long imported history materializes one payload document per
            // event behind the single authority mutex, so on a network mount
            // one unbounded replay starves every other request past its
            // deadline (2026-09-18 incident). A deadline-truncated page keeps
            // `hasMore` honest through `last < head` below and lets the caller
            // resume from the last delivered sequence; zero events is only
            // answered as `deadline_elapsed`, never as a silent caught-up page.
            if deadline_elapsed() {
                if events.is_empty() {
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        "conversation sync changes deadline elapsed",
                    ));
                }
                break 'pages;
            }
            let (_, value) = sync_attempt_reference::materialize(
                database, transaction, &key, &stored,
                MAX_LEGACY_DOCUMENT_BYTES, true)?;
            // Same resumable-prefix contract as the deadline path above: an
            // event that would push the page past its byte budget TRUNCATES
            // the page instead of failing it. A hard error here is not a
            // transient signal — replay clients re-ask for exactly one page
            // shape (the SSE route uses limit=500), so a conversation whose
            // next 500 events exceed the budget wedged every lagging stream
            // into an infinite `database_resource_exhausted` retry loop that
            // yielded only degraded heartbeats (2026-09-28 incident: frozen
            // "queued" placeholders on swarm-fat conversations). Zero events
            // is still answered loud: one event beyond the budget can never
            // make progress, and a silent empty page would read as caught-up.
            let Some(next_bytes) = response_bytes
                .checked_add(value.len() + usize::from(!events.is_empty()))
                .filter(|bytes| *bytes <= MAX_LEGACY_DOCUMENT_BYTES)
            else {
                if events.is_empty() {
                    return Err(io::Error::new(
                        io::ErrorKind::OutOfMemory,
                        "conversation sync page exceeds 8 MiB",
                    ));
                }
                break 'pages;
            };
            response_bytes = next_bytes;
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
    include!("turn_tests.rs");
}

#[cfg(test)]
mod append_settled_hotpath_tests {
    include!("turn_append_settled_hotpath_tests.rs");
}

#[cfg(test)]
mod projection_retirement_tests {
    include!("turn_projection_retirement_tests.rs");
}

pub(crate) fn main_turn_count(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    conversation_id: &str,
) -> io::Result<u64> {
    let lane_state = read_lane_state(database, transaction, conversation_id, "main")?;
    lane_count_of(
        database,
        transaction,
        conversation_id,
        "main",
        lane_state.as_ref(),
    )
}

#[cfg(test)]
mod lane_state_tests {
    use super::*;

    fn key_head(v: u64) -> Vec<u8> {
        v.to_le_bytes().to_vec()
    }

    #[test]
    fn lane_state_codec_round_trips_and_reads_legacy_rows() {
        let legacy = key_head(41);
        let state = decode_lane_state(&legacy, "legacy").unwrap();
        assert_eq!(
            state,
            LaneState {
                head: 41,
                count: None,
                last_turn_id: None
            }
        );

        let fused = LaneState {
            head: 7,
            count: Some(3),
            last_turn_id: Some("turn-xyz".to_owned()),
        };
        let encoded = encode_lane_state(&fused);
        assert_eq!(decode_lane_state(&encoded, "fused").unwrap(), fused);

        let empty_tail = LaneState {
            head: 0,
            count: Some(0),
            last_turn_id: None,
        };
        assert_eq!(
            decode_lane_state(&encode_lane_state(&empty_tail), "empty").unwrap(),
            empty_tail
        );
        // Garbage is rejected, never coerced.
        assert!(decode_lane_state(b"\xff\x00", "bad").is_err());
        assert!(decode_lane_state(&encoded[..encoded.len() - 1], "truncated").is_err());
    }
}

#[cfg(test)]
#[path = "turn_visible_settlement_tests.rs"]
mod visible_settlement_tests;

#[cfg(test)]
mod byte_window_tests {
    include!("turn_byte_window_tests.rs");
}
