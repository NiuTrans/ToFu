//! One synchronous authority coordinating entity, stream, and blob families.
//!
//! This is the semantic executor's storage boundary. It stages every family
//! before publishing exactly one WAL transaction, then advances all in-memory
//! projections to that durable sequence. It is intentionally synchronous until
//! operation compilers can be validated inside the single-writer sequencer.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Read};
use std::path::Path;
use std::sync::Arc;
use uuid::Uuid;

use crate::authority_gc::{
    collect_authority_garbage, AuthorityGarbageCollectionBudget, AuthorityGarbageCollectionMetrics,
    AuthorityGarbageCollectionMode,
};
use crate::blob::{stage_blob, BlobReader, BlobReference};
use crate::block::BlockId;
use crate::engine::HistoryCompactionMetrics;
use crate::entity::{
    EntityDatabase, EntityKey, EntityMountConsolidationProgress, EntitySnapshot,
    EntitySnapshotPinMetrics, EntityTransaction,
};
use crate::generated_tofudb_ir::{
    ATTEMPT_ID_CLAIM_NAMESPACE, BILLING_IDEMPOTENCY_NAMESPACE, BILLING_LEDGER_ID_CLAIM_NAMESPACE,
    BILLING_LEDGER_NAMESPACE, BILLING_PAYMENT_COUNT_NAMESPACE,
    BILLING_PAYMENT_CREATED_INDEX_NAMESPACE, BILLING_PAYMENT_DOCUMENT_NAMESPACE,
    BILLING_PAYMENT_ID_CLAIM_NAMESPACE, BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE,
    BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE, BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE,
    BILLING_REDEEM_CODE_LOCATOR_NAMESPACE, BILLING_REDEEM_CODE_STATE_NAMESPACE,
    BILLING_REDEEM_COUNT_NAMESPACE, BILLING_REDEEM_CREATED_INDEX_NAMESPACE,
    BILLING_RESERVE_AGE_INDEX_NAMESPACE, BILLING_RESERVE_STATE_NAMESPACE,
    BILLING_USER_AGGREGATE_NAMESPACE, BILLING_USER_TIME_INDEX_NAMESPACE, BILLING_WALLET_NAMESPACE,
    COMPACTION_ARCHIVE_ID_CLAIM_NAMESPACE, CONVERSATION_ID_CLAIM_NAMESPACE,
    CREDENTIAL_CORE_NAMESPACE, CREDENTIAL_OWNER_COUNT_NAMESPACE, CREDENTIAL_OWNER_INDEX_NAMESPACE,
    CREDENTIAL_SECRET_INDEX_NAMESPACE, CREDENTIAL_SETTINGS_NAMESPACE, CREDENTIAL_STATE_NAMESPACE,
    INTEGRATION_ACTIVE_COUNT_NAMESPACE, INTEGRATION_EVENT_COUNT_NAMESPACE,
    INTEGRATION_EVENT_NAMESPACE, INTEGRATION_EVENT_SEQUENCE_NAMESPACE,
    INTEGRATION_INTEGRATING_INDEX_NAMESPACE, INTEGRATION_NATURAL_CLAIM_NAMESPACE,
    INTEGRATION_PROJECT_ACTIVE_CLAIM_NAMESPACE, INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE,
    INTEGRATION_READY_INDEX_NAMESPACE, INTEGRATION_ROW_LOCATOR_NAMESPACE,
    INTEGRATION_ROW_SEQUENCE_NAMESPACE, INTEGRATION_WORKSPACE_COUNT_NAMESPACE,
    KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
    INTEGRATION_WORKSPACE_NAMESPACE, ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE,
    ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE, ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE,
    ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE, ORCHESTRATION_RUN_CORE_NAMESPACE,
    ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE, ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE,
    ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE, ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE,
    ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE, ORCHESTRATION_RUN_STATE_NAMESPACE,
    ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE, PROVIDER_ID_CLAIM_NAMESPACE,
    QUEUE_AUTOPILOT_MARKER_NAMESPACE, QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
    QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE, QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE,
    QUEUE_ITEM_CORE_NAMESPACE, QUEUE_ITEM_ID_CLAIM_NAMESPACE, QUEUE_ITEM_STATE_NAMESPACE,
    SCHEDULER_POLL_SEQUENCE_NAMESPACE, SCHEDULER_TASK_DOCUMENT_NAMESPACE,
    SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
    SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE, SCHEDULER_TASK_ID_CLAIM_NAMESPACE,
    SWARM_SESSION_KEY_CLAIM_NAMESPACE, TASK_RESULT_DOCUMENT_NAMESPACE,
    TASK_RESULT_HEADER_NAMESPACE, TENANT_USER_CREATED_INDEX_NAMESPACE,
    TENANT_USER_EMAIL_INDEX_NAMESPACE, TENANT_USER_OWNER_SEQUENCE_NAMESPACE,
    TENANT_USER_PROFILE_NAMESPACE, TENANT_USER_STATE_NAMESPACE, TENANT_USER_STATUS_INDEX_NAMESPACE,
    TIMER_DOCUMENT_NAMESPACE, TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE,
    TIMER_ID_CLAIM_NAMESPACE, TIMER_POLL_SEQUENCE_NAMESPACE, TURN_ID_CLAIM_NAMESPACE,
    WORKER_JOB_DOCUMENT_NAMESPACE, WORKER_JOB_IDEMPOTENCY_NAMESPACE,
    WORKER_JOB_LEASE_INDEX_NAMESPACE, WORKER_JOB_QUEUED_INDEX_NAMESPACE,
    WORKER_JOB_QUEUED_SUMMARY_NAMESPACE,
};
use crate::logical_outbox::{
    encode_logical_outbox_family_record, LogicalOutboxCapture, LogicalOutboxCipher,
    SealedLogicalOutboxRecord, StoredLogicalOutboxRecord, MAX_ENCODED_LOGICAL_OUTBOX_BYTES,
    MAX_INLINE_LOGICAL_OUTBOX_BYTES,
};
use crate::receipt::{
    command_receipt_entity_key, command_receipt_key, decode_receipt_response,
    encode_receipt_family_record, encode_receipt_response, validate_receipt_identity,
    CommandReceipt, StoredReceiptResponse, MAX_INLINE_RECEIPT_BYTES, MAX_STORED_RECEIPT_BYTES,
};
use crate::stream::{
    persisted_event_count, prepare_persisted_append, read_persisted, read_persisted_positions,
    retire_persisted_prefix, PreparedStreamAppend, StreamAppendResult, StreamEvent, StreamKey,
    StreamPage, StreamRetirementProgress,
};
use crate::transaction::{FamilyRecordKind, FamilyTransactionBuilder};
use crate::vfs::Vfs;

pub use crate::generated_tofudb_ir::MAX_TRANSACTION_STREAM_APPENDS as MAX_STREAM_APPENDS_PER_TRANSACTION;
pub const MAX_STREAM_BYTES_PER_TRANSACTION: usize = 8 * 1024 * 1024;
pub const MAX_LOGICAL_OUTBOX_FETCH_RECORDS: usize = 64;
pub const MAX_LOGICAL_OUTBOX_FETCH_BYTES: usize = 8 * 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConversationActivityBackfillProgress {
    pub processed_rows: usize,
    pub source_bytes: usize,
    pub complete: bool,
    pub committed: bool,
    pub durable_sequence: u64,
}

const LOGICAL_OUTBOX_NAMESPACE: &str = "logical_outbox";
const OUTBOX_LAST_SEQUENCE_KEY: &[u8] = b"meta:last_sequence";
const OUTBOX_PUBLISHED_SEQUENCE_KEY: &[u8] = b"meta:published_sequence";
const OUTBOX_PENDING_BYTES_KEY: &[u8] = b"meta:pending_bytes";
const OUTBOX_ENCRYPTION_KEY_ID_KEY: &[u8] = b"meta:encryption_key_id";
const OUTBOX_RECORD_PREFIX: &[u8] = b"record:";

struct PendingStreamAppend {
    key: StreamKey,
    expected_next_sequence: u64,
    events: Vec<StreamEvent>,
}

pub struct AuthorityTransaction {
    authority_uuid: Uuid,
    tenant_id: u64,
    owner_user_id: u64,
    entity: EntityTransaction,
    stream_appends: Vec<PendingStreamAppend>,
    stream_bytes: usize,
    staged_blob_block_ids: BTreeSet<BlockId>,
    receipt_record: Option<Vec<u8>>,
    logical_outbox_record: Option<Vec<u8>>,
    has_business_mutation: bool,
    content_free_diagnostic_outbox_exemption: bool,
    internal_maintenance: bool,
}

impl AuthorityTransaction {
    pub(crate) const fn tenant_id(&self) -> u64 {
        self.tenant_id
    }

    pub(crate) const fn owner_user_id(&self) -> u64 {
        self.owner_user_id
    }

    pub(crate) const fn has_business_mutation(&self) -> bool {
        self.has_business_mutation
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuthorityCommitResult {
    pub transaction_sequence: u64,
    pub stream_appends: Vec<StreamAppendResult>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LogicalOutboxStatus {
    pub last_sequence: u64,
    pub published_sequence: u64,
    pub pending_bytes: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PendingLogicalOutboxRecord {
    pub record: SealedLogicalOutboxRecord,
    pub record_bytes: u64,
}

struct LogicalOutboxRuntime {
    cipher: LogicalOutboxCipher,
    max_pending_bytes: u64,
    max_record_bytes: usize,
}

pub struct AuthorityDatabase {
    entities: EntityDatabase,
    logical_outbox: Option<LogicalOutboxRuntime>,
    timer_live_capacity: usize,
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

fn outbox_entity_key(tenant_id: u64, owner_user_id: u64, key: &[u8]) -> io::Result<EntityKey> {
    EntityKey::new(tenant_id, owner_user_id, LOGICAL_OUTBOX_NAMESPACE, key)
}

fn outbox_record_key(tenant_id: u64, owner_user_id: u64, sequence: u64) -> io::Result<EntityKey> {
    let mut key = Vec::with_capacity(OUTBOX_RECORD_PREFIX.len() + 8);
    key.extend_from_slice(OUTBOX_RECORD_PREFIX);
    key.extend_from_slice(&sequence.to_be_bytes());
    outbox_entity_key(tenant_id, owner_user_id, &key)
}

fn decode_counter(value: Option<Vec<u8>>, name: &str) -> io::Result<u64> {
    match value {
        None => Ok(0),
        Some(value) if value.len() == 8 => Ok(u64::from_le_bytes(value.try_into().unwrap())),
        Some(_) => Err(invalid_data(name)),
    }
}

impl AuthorityDatabase {
    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        let entities = EntityDatabase::initialize(data_dir)?;
        Ok(Self {
            entities,
            logical_outbox: None,
            timer_live_capacity: 16,
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        let entities = EntityDatabase::open(data_dir)?;
        Ok(Self {
            entities,
            logical_outbox: None,
            timer_live_capacity: 16,
        })
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let entities = EntityDatabase::initialize_with_vfs(data_dir, vfs)?;
        Ok(Self {
            entities,
            logical_outbox: None,
            timer_live_capacity: 16,
        })
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let entities = EntityDatabase::open_with_vfs(data_dir, vfs)?;
        Ok(Self {
            entities,
            logical_outbox: None,
            timer_live_capacity: 16,
        })
    }

    pub fn configure_logical_outbox(
        &mut self,
        encryption_key: &[u8; 32],
        max_pending_bytes: u64,
        max_record_bytes: usize,
    ) -> io::Result<()> {
        if max_pending_bytes == 0
            || max_record_bytes == 0
            || max_record_bytes > MAX_ENCODED_LOGICAL_OUTBOX_BYTES
            || max_record_bytes as u64 > max_pending_bytes
        {
            return Err(invalid_input("invalid logical outbox resource budget"));
        }
        self.logical_outbox = Some(LogicalOutboxRuntime {
            cipher: LogicalOutboxCipher::new(encryption_key),
            max_pending_bytes,
            max_record_bytes,
        });
        Ok(())
    }

    pub(crate) fn configure_timer_live_capacity(&mut self, capacity: usize) -> io::Result<()> {
        if !(1..=crate::generated_tofudb_ir::MAX_ACTIVE_TIMERS_PER_OWNER_HARD_CEILING)
            .contains(&capacity)
        {
            return Err(invalid_input("invalid timer live capacity"));
        }
        self.timer_live_capacity = capacity;
        Ok(())
    }

    pub(crate) fn timer_live_capacity(&self) -> usize {
        self.timer_live_capacity
    }

    pub fn compact_history(
        &mut self,
        maximum_retained_segments: usize,
    ) -> io::Result<HistoryCompactionMetrics> {
        self.entities
            .engine_mut()
            .compact_history(maximum_retained_segments)
    }

    #[cfg(test)]
    pub(crate) fn engine_mut_for_test(&mut self) -> &mut crate::engine::Engine {
        self.entities.engine_mut()
    }

    pub(crate) fn compact_history_if_checkpointed(
        &mut self,
        maximum_retained_segments: usize,
    ) -> io::Result<Option<HistoryCompactionMetrics>> {
        let engine = self.entities.engine_mut();
        if !engine.committed_transactions().is_empty()
            || engine.history_segment_count() <= maximum_retained_segments
        {
            return Ok(None);
        }
        engine.compact_history(maximum_retained_segments).map(Some)
    }

    pub fn collect_garbage(
        &mut self,
        budget: AuthorityGarbageCollectionBudget,
    ) -> io::Result<AuthorityGarbageCollectionMetrics> {
        self.garbage_collection(budget, AuthorityGarbageCollectionMode::Execute)
    }

    pub fn plan_garbage_collection(
        &mut self,
        budget: AuthorityGarbageCollectionBudget,
    ) -> io::Result<AuthorityGarbageCollectionMetrics> {
        self.garbage_collection(budget, AuthorityGarbageCollectionMode::Plan)
    }

    fn garbage_collection(
        &mut self,
        budget: AuthorityGarbageCollectionBudget,
        mode: AuthorityGarbageCollectionMode,
    ) -> io::Result<AuthorityGarbageCollectionMetrics> {
        if self.snapshot_pin_metrics()?.active_handles != 0 {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "authority GC defers while MVCC snapshots are active",
            ));
        }
        collect_authority_garbage(self.entities.engine_mut(), budget, mode)
    }

    #[cfg(test)]
    pub(crate) fn authority_state_root(&self) -> Option<BlockId> {
        self.entities
            .engine()
            .state()
            .authority_state_root
            .flatten()
    }

    #[cfg(test)]
    pub(crate) fn checkpoint_for_test(&mut self) -> io::Result<u64> {
        self.entities.engine_mut().checkpoint()
    }

    #[cfg(test)]
    pub(crate) fn history_segment_count_for_test(&self) -> usize {
        self.entities.engine().history_segment_count()
    }

    #[cfg(test)]
    pub(crate) fn write_orphan_block_for_test(&self, payload: &[u8]) -> io::Result<BlockId> {
        self.entities.engine().write_block(payload)
    }

    #[cfg(test)]
    pub(crate) fn read_block_for_test(&self, block_id: BlockId) -> io::Result<Vec<u8>> {
        self.entities.engine().read_block(block_id)
    }

    #[cfg(test)]
    pub(crate) fn commit_block_payload_for_test(&mut self, payload: &[u8]) -> io::Result<BlockId> {
        Ok(self
            .entities
            .engine_mut()
            .commit_transaction(b"gc-payload-reference", &[payload])?
            .block_ids[0])
    }

    #[cfg(test)]
    pub(crate) fn commit_block_payloads_for_test(
        &mut self,
        payloads: &[Vec<u8>],
    ) -> io::Result<Vec<BlockId>> {
        let payload_slices = payloads.iter().map(Vec::as_slice).collect::<Vec<_>>();
        Ok(self
            .entities
            .engine_mut()
            .commit_transaction(b"gc-payload-references", &payload_slices)?
            .block_ids)
    }

    #[cfg(test)]
    pub(crate) fn compact_payload_blocks_for_test(
        &mut self,
        block_ids: &[BlockId],
    ) -> io::Result<()> {
        self.entities
            .engine_mut()
            .compact_payload_blocks(block_ids)
            .map(|_| ())
    }

    #[cfg(test)]
    pub(crate) fn payload_segment_count_for_test(&self) -> usize {
        self.entities.engine().payload_segment_count()
    }

    #[cfg(test)]
    pub(crate) fn create_orphan_payload_segment_for_test(
        &self,
        block_id: BlockId,
    ) -> io::Result<Uuid> {
        self.entities
            .engine()
            .create_orphan_payload_segment_for_test(block_id)
    }

    pub fn begin(&self, tenant_id: u64, owner_user_id: u64) -> io::Result<AuthorityTransaction> {
        self.begin_with_entity_scopes(tenant_id, owner_user_id, Vec::new())
    }

    pub fn backfill_conversation_activity_candidates(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        maximum_rows: usize,
    ) -> io::Result<ConversationActivityBackfillProgress> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input(
                "activity backfill scope IDs must be positive",
            ));
        }
        let mut transaction = self.begin(tenant_id, owner_user_id)?;
        let batch = crate::conversation_header::backfill_activity_candidates(
            self,
            &mut transaction,
            maximum_rows,
        )?;
        let committed = batch.changed;
        let durable_sequence = if committed {
            self.commit(transaction)?.transaction_sequence
        } else {
            self.entities.engine().state().durable_sequence
        };
        Ok(ConversationActivityBackfillProgress {
            processed_rows: batch.processed_rows,
            source_bytes: batch.source_bytes,
            complete: batch.complete,
            committed,
            durable_sequence,
        })
    }

    pub(crate) fn begin_with_identity_claim_scopes(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
    ) -> io::Result<AuthorityTransaction> {
        let additional_scope_prefixes = [
            CONVERSATION_ID_CLAIM_NAMESPACE,
            TURN_ID_CLAIM_NAMESPACE,
            ATTEMPT_ID_CLAIM_NAMESPACE,
            crate::generated_tofudb_ir::ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE,
            COMPACTION_ARCHIVE_ID_CLAIM_NAMESPACE,
            PROVIDER_ID_CLAIM_NAMESPACE,
            ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE,
            ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE,
            ORCHESTRATION_RUN_CORE_NAMESPACE,
            ORCHESTRATION_RUN_STATE_NAMESPACE,
            ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE,
            ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE,
            ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE,
            ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE,
            ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE,
            ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE,
            ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE,
            ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE,
            crate::generated_tofudb_ir::PAPER_PODCAST_CORE_NAMESPACE,
            crate::generated_tofudb_ir::PAPER_PODCAST_STATE_NAMESPACE,
            crate::generated_tofudb_ir::PAPER_PODCAST_COUNT_NAMESPACE,
            crate::generated_tofudb_ir::PAPER_PODCAST_ACTIVE_INDEX_NAMESPACE,
            crate::generated_tofudb_ir::PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE,
            INTEGRATION_WORKSPACE_NAMESPACE,
            INTEGRATION_NATURAL_CLAIM_NAMESPACE,
            INTEGRATION_ROW_LOCATOR_NAMESPACE,
            INTEGRATION_ROW_SEQUENCE_NAMESPACE,
            INTEGRATION_READY_INDEX_NAMESPACE,
            INTEGRATION_INTEGRATING_INDEX_NAMESPACE,
            INTEGRATION_PROJECT_ACTIVE_CLAIM_NAMESPACE,
            INTEGRATION_ACTIVE_COUNT_NAMESPACE,
            INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE,
            INTEGRATION_WORKSPACE_COUNT_NAMESPACE,
            INTEGRATION_EVENT_NAMESPACE,
            INTEGRATION_EVENT_SEQUENCE_NAMESPACE,
            INTEGRATION_EVENT_COUNT_NAMESPACE,
            QUEUE_ITEM_CORE_NAMESPACE,
            QUEUE_ITEM_STATE_NAMESPACE,
            QUEUE_ITEM_ID_CLAIM_NAMESPACE,
            QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE,
            QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE,
            QUEUE_AUTOPILOT_MARKER_NAMESPACE,
            QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE,
            SCHEDULER_TASK_DOCUMENT_NAMESPACE,
            SCHEDULER_TASK_ID_CLAIM_NAMESPACE,
            SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
            SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE,
            SCHEDULER_POLL_SEQUENCE_NAMESPACE,
            SWARM_SESSION_KEY_CLAIM_NAMESPACE,
            TIMER_DOCUMENT_NAMESPACE,
            TIMER_ID_CLAIM_NAMESPACE,
            TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE,
            TIMER_POLL_SEQUENCE_NAMESPACE,
            TASK_RESULT_DOCUMENT_NAMESPACE,
            TASK_RESULT_HEADER_NAMESPACE,
            WORKER_JOB_DOCUMENT_NAMESPACE,
            WORKER_JOB_IDEMPOTENCY_NAMESPACE,
            WORKER_JOB_LEASE_INDEX_NAMESPACE,
            WORKER_JOB_QUEUED_INDEX_NAMESPACE,
            WORKER_JOB_QUEUED_SUMMARY_NAMESPACE,
            TENANT_USER_PROFILE_NAMESPACE,
            TENANT_USER_STATE_NAMESPACE,
            TENANT_USER_EMAIL_INDEX_NAMESPACE,
            TENANT_USER_CREATED_INDEX_NAMESPACE,
            TENANT_USER_STATUS_INDEX_NAMESPACE,
            TENANT_USER_OWNER_SEQUENCE_NAMESPACE,
            CREDENTIAL_CORE_NAMESPACE,
            CREDENTIAL_SETTINGS_NAMESPACE,
            CREDENTIAL_STATE_NAMESPACE,
            CREDENTIAL_SECRET_INDEX_NAMESPACE,
            CREDENTIAL_OWNER_INDEX_NAMESPACE,
            CREDENTIAL_OWNER_COUNT_NAMESPACE,
            BILLING_WALLET_NAMESPACE,
            BILLING_LEDGER_NAMESPACE,
            BILLING_LEDGER_ID_CLAIM_NAMESPACE,
            BILLING_IDEMPOTENCY_NAMESPACE,
            BILLING_USER_TIME_INDEX_NAMESPACE,
            BILLING_USER_AGGREGATE_NAMESPACE,
            BILLING_RESERVE_STATE_NAMESPACE,
            BILLING_RESERVE_AGE_INDEX_NAMESPACE,
            BILLING_PAYMENT_DOCUMENT_NAMESPACE,
            BILLING_PAYMENT_ID_CLAIM_NAMESPACE,
            BILLING_PAYMENT_PROVIDER_CLAIM_NAMESPACE,
            BILLING_PAYMENT_CREATED_INDEX_NAMESPACE,
            BILLING_PAYMENT_COUNT_NAMESPACE,
            BILLING_REDEEM_BATCH_DOCUMENT_NAMESPACE,
            BILLING_REDEEM_CODE_LOCATOR_NAMESPACE,
            BILLING_REDEEM_CODE_STATE_NAMESPACE,
            BILLING_REDEEM_CREATED_INDEX_NAMESPACE,
            BILLING_REDEEM_BATCH_CREATED_INDEX_NAMESPACE,
            BILLING_REDEEM_COUNT_NAMESPACE,
            crate::raw_archive::ID_CLAIM_NAMESPACE,
            crate::raw_archive::TENANT_USAGE_NAMESPACE,
            KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
        ]
        .into_iter()
        .map(|namespace| {
            EntityKey::new(
                tenant_id,
                crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
                namespace,
                b"",
            )
            .map(|key| key.encoded().to_vec())
        })
        .collect::<io::Result<Vec<_>>>()?;
        self.begin_with_entity_scopes(tenant_id, owner_user_id, additional_scope_prefixes)
    }

    fn begin_with_entity_scopes(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        additional_scope_prefixes: Vec<Vec<u8>>,
    ) -> io::Result<AuthorityTransaction> {
        Ok(AuthorityTransaction {
            authority_uuid: self.entities.engine().state().authority_uuid,
            tenant_id,
            owner_user_id,
            entity: self.entities.begin_with_additional_scope_prefixes(
                tenant_id,
                owner_user_id,
                additional_scope_prefixes,
            )?,
            stream_appends: Vec::new(),
            stream_bytes: 0,
            staged_blob_block_ids: BTreeSet::new(),
            receipt_record: None,
            logical_outbox_record: None,
            has_business_mutation: false,
            content_free_diagnostic_outbox_exemption: false,
            internal_maintenance: false,
        })
    }

    pub fn snapshot_pin_metrics(&self) -> io::Result<EntitySnapshotPinMetrics> {
        self.entities.snapshot_pin_metrics()
    }

    pub fn stage_persistent_snapshot_pin(
        &self,
        transaction: &mut AuthorityTransaction,
        pin_id: &[u8],
    ) -> io::Result<EntitySnapshot> {
        self.entities
            .stage_persistent_snapshot_pin(&mut transaction.entity, pin_id)
    }

    pub fn stage_persistent_range_snapshot_pin(
        &self,
        transaction: &mut AuthorityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        self.entities
            .stage_persistent_range_snapshot_pin(&mut transaction.entity, pin_id, ranges)
    }

    pub fn stage_persistent_range_snapshot_restore(
        &self,
        transaction: &mut AuthorityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        let snapshot = self.entities.stage_persistent_range_snapshot_restore(
            &mut transaction.entity,
            pin_id,
            ranges,
        )?;
        transaction.has_business_mutation = true;
        Ok(snapshot)
    }

    pub fn remove_persistent_snapshot_pin(
        &self,
        transaction: &mut AuthorityTransaction,
        pin_id: &[u8],
    ) -> io::Result<bool> {
        self.entities
            .remove_persistent_snapshot_pin(&mut transaction.entity, pin_id)
    }

    pub fn begin_persistent_snapshot(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        pin_id: &[u8],
    ) -> io::Result<Option<AuthorityTransaction>> {
        let Some(entity) =
            self.entities
                .begin_persistent_snapshot(tenant_id, owner_user_id, pin_id)?
        else {
            return Ok(None);
        };
        Ok(Some(AuthorityTransaction {
            authority_uuid: self.entities.engine().state().authority_uuid,
            tenant_id,
            owner_user_id,
            entity,
            stream_appends: Vec::new(),
            stream_bytes: 0,
            staged_blob_block_ids: BTreeSet::new(),
            receipt_record: None,
            logical_outbox_record: None,
            has_business_mutation: false,
            content_free_diagnostic_outbox_exemption: false,
            internal_maintenance: false,
        }))
    }

    pub fn entity_get(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &EntityKey,
    ) -> io::Result<Option<Vec<u8>>> {
        self.entities.get(&mut transaction.entity, key)
    }

    pub(crate) fn authorize_entity_namespace_for_owner(
        &self,
        transaction: &mut AuthorityTransaction,
        owner_user_id: u64,
        namespace: &str,
    ) -> io::Result<()> {
        let prefix = EntityKey::new(transaction.tenant_id, owner_user_id, namespace, b"")?;
        self.entities
            .authorize_additional_scope_prefix(&mut transaction.entity, prefix.encoded().to_vec())
    }

    pub fn entity_scan(
        &self,
        transaction: &mut AuthorityTransaction,
        start: &EntityKey,
        end: &EntityKey,
        limit: usize,
    ) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.entities
            .scan(&mut transaction.entity, start, end, limit)
    }

    pub fn entity_scan_reverse(
        &self,
        transaction: &mut AuthorityTransaction,
        start: &EntityKey,
        end: &EntityKey,
        limit: usize,
    ) -> io::Result<Vec<(EntityKey, Vec<u8>)>> {
        self.entities
            .scan_reverse(&mut transaction.entity, start, end, limit)
    }

    pub fn entity_put(
        &self,
        transaction: &mut AuthorityTransaction,
        key: EntityKey,
        value: Vec<u8>,
    ) -> io::Result<()> {
        self.entities.put(&mut transaction.entity, key, value)?;
        transaction.has_business_mutation = true;
        Ok(())
    }

    pub fn entity_delete(
        &self,
        transaction: &mut AuthorityTransaction,
        key: EntityKey,
    ) -> io::Result<()> {
        self.entities.delete(&mut transaction.entity, key)?;
        transaction.has_business_mutation = true;
        Ok(())
    }

    pub(crate) fn maintenance_entity_put(
        &self,
        transaction: &mut AuthorityTransaction,
        key: EntityKey,
        value: Vec<u8>,
    ) -> io::Result<()> {
        if transaction.has_business_mutation {
            return Err(invalid_input(
                "physical maintenance cannot join a business transaction",
            ));
        }
        self.entities.put(&mut transaction.entity, key, value)?;
        transaction.internal_maintenance = true;
        Ok(())
    }

    pub(crate) fn maintenance_entity_delete(
        &self,
        transaction: &mut AuthorityTransaction,
        key: EntityKey,
    ) -> io::Result<()> {
        if transaction.has_business_mutation {
            return Err(invalid_input(
                "physical maintenance cannot join a business transaction",
            ));
        }
        self.entities.delete(&mut transaction.entity, key)?;
        transaction.internal_maintenance = true;
        Ok(())
    }

    pub fn entity_retire_range(
        &self,
        transaction: &mut AuthorityTransaction,
        start: &EntityKey,
        end: &EntityKey,
    ) -> io::Result<()> {
        self.entities
            .retire_range(&mut transaction.entity, start, end)?;
        transaction.has_business_mutation = true;
        Ok(())
    }

    pub fn consolidate_one_entity_range_mount(
        &self,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<Option<EntityMountConsolidationProgress>> {
        self.entities
            .consolidate_one_range_mount(&mut transaction.entity)
    }

    pub fn stream_append(
        &self,
        transaction: &mut AuthorityTransaction,
        key: StreamKey,
        expected_next_sequence: u64,
        events: Vec<StreamEvent>,
    ) -> io::Result<()> {
        let existing_append = transaction
            .stream_appends
            .iter()
            .position(|append| append.key == key);
        if existing_append.is_none()
            && transaction.stream_appends.len() == MAX_STREAM_APPENDS_PER_TRANSACTION
        {
            return Err(invalid_input(
                "authority transaction has too many stream appends",
            ));
        }
        let append_bytes = events.iter().try_fold(0_usize, |total, event| {
            total
                .checked_add(event.encoded_len())
                .ok_or_else(|| invalid_input("authority stream byte count overflow"))
        })?;
        let next_stream_bytes = transaction
            .stream_bytes
            .checked_add(append_bytes)
            .ok_or_else(|| invalid_input("authority stream byte count overflow"))?;
        if next_stream_bytes > MAX_STREAM_BYTES_PER_TRANSACTION {
            return Err(invalid_input(
                "authority transaction stream payload exceeds 8 MiB",
            ));
        }
        if let Some(index) = existing_append {
            let pending = &mut transaction.stream_appends[index];
            let pending_next = pending
                .expected_next_sequence
                .checked_add(pending.events.len() as u64)
                .ok_or_else(|| invalid_data("pending stream sequence overflow"))?;
            if expected_next_sequence != pending_next {
                return Err(conflict("pending stream expected-position witness changed"));
            }
            if pending.events.len() + events.len()
                > crate::generated_tofudb_ir::MAX_STREAM_APPEND_EVENTS
            {
                return Err(invalid_input(
                    "pending stream event count exceeds its bound",
                ));
            }
            pending.events.extend(events);
            transaction.stream_bytes = next_stream_bytes;
            transaction.has_business_mutation = true;
            return Ok(());
        }
        transaction.stream_appends.push(PendingStreamAppend {
            key,
            expected_next_sequence,
            events,
        });
        transaction.stream_bytes = next_stream_bytes;
        transaction.has_business_mutation = true;
        Ok(())
    }

    pub fn stream_next_sequence(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
    ) -> io::Result<u64> {
        let mut transaction = self.entities.begin(tenant_id, owner_user_id)?;
        persisted_event_count(&self.entities, &mut transaction, key)?
            .checked_add(1)
            .ok_or_else(|| invalid_data("persisted stream sequence overflow"))
    }

    pub fn transaction_stream_next_sequence(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &StreamKey,
    ) -> io::Result<u64> {
        let committed = persisted_event_count(&self.entities, &mut transaction.entity, key)?
            .checked_add(1)
            .ok_or_else(|| invalid_data("persisted stream sequence overflow"))?;
        let pending = transaction
            .stream_appends
            .iter()
            .find(|append| &append.key == key)
            .map_or(0, |append| append.events.len() as u64);
        committed
            .checked_add(pending)
            .ok_or_else(|| invalid_data("pending stream sequence overflow"))
    }

    pub fn stage_blob<R: Read>(
        &self,
        transaction: &mut AuthorityTransaction,
        reader: &mut R,
        maximum_bytes: u64,
    ) -> io::Result<BlobReference> {
        if !EntityDatabase::transaction_is_writable(&transaction.entity) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        let staged = stage_blob(
            self.entities.engine(),
            transaction.tenant_id,
            transaction.owner_user_id,
            reader,
            maximum_bytes,
        )?;
        self.attach_staged_blob(transaction, staged)
    }

    pub(crate) fn stage_tenant_global_blob<R: Read>(
        &self,
        transaction: &mut AuthorityTransaction,
        reader: &mut R,
        maximum_bytes: u64,
    ) -> io::Result<BlobReference> {
        if !EntityDatabase::transaction_is_writable(&transaction.entity) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        let staged = stage_blob(
            self.entities.engine(),
            transaction.tenant_id,
            crate::conversation_header::TENANT_GLOBAL_OWNER_ID,
            reader,
            maximum_bytes,
        )?;
        self.attach_staged_blob(transaction, staged)
    }

    fn attach_staged_blob(
        &self,
        transaction: &mut AuthorityTransaction,
        staged: crate::blob::StagedBlob,
    ) -> io::Result<BlobReference> {
        let additional_block_ids = staged
            .transaction_block_ids
            .iter()
            .filter(|block_id| !transaction.staged_blob_block_ids.contains(block_id))
            .count();
        if transaction.staged_blob_block_ids.len() + additional_block_ids
            > crate::transaction::MAX_REFERENCED_BLOCKS
        {
            return Err(invalid_input(
                "authority transaction stages too many blob blocks",
            ));
        }
        transaction
            .staged_blob_block_ids
            .extend(staged.transaction_block_ids);
        Ok(staged.reference)
    }

    pub fn stream_read(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
        from_sequence: u64,
        limit: usize,
    ) -> io::Result<StreamPage> {
        let mut transaction = self.entities.begin(tenant_id, owner_user_id)?;
        read_persisted(
            self.entities.engine(),
            &self.entities,
            &mut transaction,
            key,
            from_sequence,
            limit,
        )
    }

    pub fn stream_read_positions(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        key: &StreamKey,
        positions: &BTreeSet<u64>,
    ) -> io::Result<BTreeMap<u64, StreamEvent>> {
        let mut transaction = self.entities.begin(tenant_id, owner_user_id)?;
        read_persisted_positions(
            self.entities.engine(),
            &self.entities,
            &mut transaction,
            key,
            positions,
        )
    }

    pub(crate) fn stream_read_in_transaction(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &StreamKey,
        from_sequence: u64,
        limit: usize,
    ) -> io::Result<StreamPage> {
        read_persisted(
            self.entities.engine(),
            &self.entities,
            &mut transaction.entity,
            key,
            from_sequence,
            limit,
        )
    }

    pub(crate) fn stream_read_positions_in_transaction(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &StreamKey,
        positions: &BTreeSet<u64>,
    ) -> io::Result<BTreeMap<u64, StreamEvent>> {
        read_persisted_positions(
            self.entities.engine(),
            &self.entities,
            &mut transaction.entity,
            key,
            positions,
        )
    }

    pub(crate) fn stream_retire_prefix(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &StreamKey,
        retain_from_sequence: u64,
    ) -> io::Result<StreamRetirementProgress> {
        retire_persisted_prefix(
            &self.entities,
            &mut transaction.entity,
            key,
            retain_from_sequence,
        )
    }

    pub fn blob_reader(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<BlobReader<'_>> {
        BlobReader::open(self.entities.engine(), tenant_id, owner_user_id, reference)
    }

    pub(crate) fn reachable_blob_reader(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<BlobReader<'_>> {
        BlobReader::open_reachable(self.entities.engine(), tenant_id, owner_user_id, reference)
    }

    fn outbox_counter(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &[u8],
        invalid_message: &str,
    ) -> io::Result<u64> {
        let key = outbox_entity_key(transaction.tenant_id, transaction.owner_user_id, key)?;
        decode_counter(
            self.entities.get(&mut transaction.entity, &key)?,
            invalid_message,
        )
    }

    fn put_outbox_counter(
        &self,
        transaction: &mut AuthorityTransaction,
        key: &[u8],
        value: u64,
    ) -> io::Result<()> {
        let key = outbox_entity_key(transaction.tenant_id, transaction.owner_user_id, key)?;
        self.entities
            .put(&mut transaction.entity, key, value.to_le_bytes().to_vec())
    }

    fn verify_outbox_key_identity(
        &self,
        transaction: &mut AuthorityTransaction,
        allow_initialize: bool,
    ) -> io::Result<()> {
        let runtime = self
            .logical_outbox
            .as_ref()
            .ok_or_else(|| invalid_input("logical outbox is not configured"))?;
        let key = outbox_entity_key(
            transaction.tenant_id,
            transaction.owner_user_id,
            OUTBOX_ENCRYPTION_KEY_ID_KEY,
        )?;
        match self.entities.get(&mut transaction.entity, &key)? {
            Some(key_id) if key_id == runtime.cipher.key_id() => Ok(()),
            Some(_) => Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "logical outbox encryption key identity differs",
            )),
            None if allow_initialize => self.entities.put(
                &mut transaction.entity,
                key,
                runtime.cipher.key_id().to_vec(),
            ),
            None => Err(invalid_data(
                "logical outbox encryption key identity is missing",
            )),
        }
    }

    pub fn logical_outbox_capture(
        &self,
        transaction: &mut AuthorityTransaction,
        capture: LogicalOutboxCapture,
    ) -> io::Result<u64> {
        if transaction.logical_outbox_record.is_some() {
            return Err(invalid_input(
                "authority transaction contains more than one logical outbox record",
            ));
        }
        let runtime = self
            .logical_outbox
            .as_ref()
            .ok_or_else(|| invalid_input("logical outbox is not configured"))?;
        self.verify_outbox_key_identity(transaction, true)?;
        let last_sequence = self.outbox_counter(
            transaction,
            OUTBOX_LAST_SEQUENCE_KEY,
            "invalid logical outbox last sequence",
        )?;
        let pending_bytes = self.outbox_counter(
            transaction,
            OUTBOX_PENDING_BYTES_KEY,
            "invalid logical outbox pending byte count",
        )?;
        let sequence = last_sequence
            .checked_add(1)
            .ok_or_else(|| invalid_data("logical outbox sequence overflow"))?;
        let (identity, clear_payload) =
            capture.into_identity(sequence, transaction.tenant_id, transaction.owner_user_id)?;
        let sealed = runtime.cipher.seal(identity, &clear_payload)?;
        let encoded_record = sealed.encode()?;
        if encoded_record.len() > runtime.max_record_bytes {
            return Err(invalid_input(
                "logical outbox record exceeds its configured record budget",
            ));
        }
        let record_bytes = encoded_record.len() as u64;
        let next_pending_bytes = pending_bytes
            .checked_add(record_bytes)
            .filter(|value| *value <= runtime.max_pending_bytes)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "logical outbox reached its configured pending byte budget",
                )
            })?;
        let stored = if encoded_record.len() <= MAX_INLINE_LOGICAL_OUTBOX_BYTES {
            StoredLogicalOutboxRecord::Inline(encoded_record)
        } else {
            StoredLogicalOutboxRecord::Blob(self.stage_blob(
                transaction,
                &mut encoded_record.as_slice(),
                record_bytes,
            )?)
        };
        let stored_entry = stored.encode()?;
        let record_key =
            outbox_record_key(transaction.tenant_id, transaction.owner_user_id, sequence)?;
        if self
            .entities
            .get(&mut transaction.entity, &record_key)?
            .is_some()
        {
            return Err(invalid_data("logical outbox sequence already exists"));
        }
        self.entities
            .put(&mut transaction.entity, record_key, stored_entry)?;
        self.put_outbox_counter(transaction, OUTBOX_LAST_SEQUENCE_KEY, sequence)?;
        self.put_outbox_counter(transaction, OUTBOX_PENDING_BYTES_KEY, next_pending_bytes)?;
        transaction.logical_outbox_record = Some(encode_logical_outbox_family_record(
            transaction.tenant_id,
            transaction.owner_user_id,
            sequence,
            sealed.event_id,
            &stored,
        )?);
        Ok(sequence)
    }

    pub(crate) fn exempt_content_free_diagnostic_from_logical_outbox(
        &self,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<()> {
        if transaction.logical_outbox_record.is_some()
            || transaction.content_free_diagnostic_outbox_exemption
        {
            return Err(invalid_input("invalid diagnostic outbox exemption"));
        }
        transaction.content_free_diagnostic_outbox_exemption = true;
        Ok(())
    }

    fn logical_outbox_status_in(
        &self,
        transaction: &mut AuthorityTransaction,
    ) -> io::Result<LogicalOutboxStatus> {
        let last_sequence = self.outbox_counter(
            transaction,
            OUTBOX_LAST_SEQUENCE_KEY,
            "invalid logical outbox last sequence",
        )?;
        let published_sequence = self.outbox_counter(
            transaction,
            OUTBOX_PUBLISHED_SEQUENCE_KEY,
            "invalid logical outbox published sequence",
        )?;
        let pending_bytes = self.outbox_counter(
            transaction,
            OUTBOX_PENDING_BYTES_KEY,
            "invalid logical outbox pending byte count",
        )?;
        if published_sequence > last_sequence
            || (published_sequence == last_sequence) != (pending_bytes == 0)
        {
            return Err(invalid_data("logical outbox metadata is inconsistent"));
        }
        Ok(LogicalOutboxStatus {
            last_sequence,
            published_sequence,
            pending_bytes,
        })
    }

    pub fn logical_outbox_status(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
    ) -> io::Result<LogicalOutboxStatus> {
        let mut transaction = self.begin(tenant_id, owner_user_id)?;
        let status = self.logical_outbox_status_in(&mut transaction)?;
        if status.last_sequence > 0 {
            self.verify_outbox_key_identity(&mut transaction, false)?;
        }
        Ok(status)
    }

    fn load_stored_logical_outbox(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        stored: &StoredLogicalOutboxRecord,
    ) -> io::Result<Vec<u8>> {
        match stored {
            StoredLogicalOutboxRecord::Inline(encoded) => Ok(encoded.clone()),
            StoredLogicalOutboxRecord::Blob(reference) => {
                let mut reader =
                    self.reachable_blob_reader(tenant_id, owner_user_id, *reference)?;
                let mut encoded = Vec::with_capacity(reference.logical_bytes as usize);
                while let Some(chunk) = reader.next_chunk()? {
                    encoded.extend_from_slice(&chunk);
                    if encoded.len() > MAX_ENCODED_LOGICAL_OUTBOX_BYTES {
                        return Err(invalid_data("logical outbox blob exceeds its byte budget"));
                    }
                }
                if encoded.len() != stored.logical_bytes() {
                    return Err(invalid_data("logical outbox blob length differs"));
                }
                Ok(encoded)
            }
        }
    }

    pub fn logical_outbox_pending(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        limit: usize,
    ) -> io::Result<Vec<PendingLogicalOutboxRecord>> {
        self.logical_outbox_pending_bounded(
            tenant_id,
            owner_user_id,
            limit,
            MAX_LOGICAL_OUTBOX_FETCH_BYTES,
        )
    }

    pub fn logical_outbox_pending_bounded(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        limit: usize,
        maximum_bytes: usize,
    ) -> io::Result<Vec<PendingLogicalOutboxRecord>> {
        if limit == 0 || limit > MAX_LOGICAL_OUTBOX_FETCH_RECORDS {
            return Err(invalid_input("invalid logical outbox fetch limit"));
        }
        if maximum_bytes == 0 || maximum_bytes > MAX_LOGICAL_OUTBOX_FETCH_BYTES {
            return Err(invalid_input("invalid logical outbox fetch byte limit"));
        }
        let mut transaction = self.begin(tenant_id, owner_user_id)?;
        let status = self.logical_outbox_status_in(&mut transaction)?;
        if status.last_sequence == status.published_sequence {
            return Ok(Vec::new());
        }
        self.verify_outbox_key_identity(&mut transaction, false)?;
        let first = status
            .published_sequence
            .checked_add(1)
            .ok_or_else(|| invalid_data("logical outbox sequence overflow"))?;
        let start = outbox_record_key(tenant_id, owner_user_id, first)?;
        let end = outbox_record_key(tenant_id, owner_user_id, u64::MAX)?;
        let rows = self.entity_scan(&mut transaction, &start, &end, limit)?;
        let mut pending = Vec::with_capacity(rows.len());
        let mut fetched_bytes = 0_usize;
        for (_, encoded_stored) in rows {
            let stored = StoredLogicalOutboxRecord::decode(&encoded_stored)?;
            let next_fetched_bytes = fetched_bytes
                .checked_add(stored.logical_bytes())
                .ok_or_else(|| invalid_data("logical outbox fetch byte count overflow"))?;
            if next_fetched_bytes > maximum_bytes {
                if pending.is_empty() {
                    return Err(invalid_input(
                        "first logical outbox record exceeds the fetch byte limit",
                    ));
                }
                break;
            }
            let expected_sequence = first + pending.len() as u64;
            let encoded_record =
                self.load_stored_logical_outbox(tenant_id, owner_user_id, &stored)?;
            let record = SealedLogicalOutboxRecord::decode(&encoded_record)?;
            if record.identity.sequence != expected_sequence
                || record.identity.tenant_id != tenant_id
                || record.identity.owner_user_id != owner_user_id
            {
                return Err(invalid_data(
                    "logical outbox pending sequence or scope differs",
                ));
            }
            pending.push(PendingLogicalOutboxRecord {
                record,
                record_bytes: encoded_record.len() as u64,
            });
            fetched_bytes = next_fetched_bytes;
        }
        if pending.is_empty() {
            return Err(invalid_data("logical outbox pending record is missing"));
        }
        Ok(pending)
    }

    pub fn is_restart_required(&self) -> bool {
        self.entities.engine().is_restart_required()
    }

    pub fn logical_outbox_is_configured(&self) -> bool {
        self.logical_outbox.is_some()
    }

    pub fn logical_outbox_decrypt(
        &self,
        record: &SealedLogicalOutboxRecord,
    ) -> io::Result<Vec<u8>> {
        self.logical_outbox
            .as_ref()
            .ok_or_else(|| invalid_input("logical outbox is not configured"))?
            .cipher
            .open(record)
    }

    pub fn logical_outbox_acknowledge(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        sequence: u64,
        event_id: [u8; 32],
    ) -> io::Result<LogicalOutboxStatus> {
        if sequence == 0 {
            return Err(invalid_input(
                "logical outbox acknowledgement sequence is zero",
            ));
        }
        let mut transaction = self.begin(tenant_id, owner_user_id)?;
        let status = self.logical_outbox_status_in(&mut transaction)?;
        if sequence <= status.published_sequence {
            return Ok(status);
        }
        self.verify_outbox_key_identity(&mut transaction, false)?;
        if sequence != status.published_sequence + 1 {
            return Err(invalid_data(
                "logical outbox acknowledgement is out of order",
            ));
        }
        let record_key = outbox_record_key(tenant_id, owner_user_id, sequence)?;
        let encoded_stored = self
            .entities
            .get(&mut transaction.entity, &record_key)?
            .ok_or_else(|| invalid_data("logical outbox record disappeared before ACK"))?;
        let stored = StoredLogicalOutboxRecord::decode(&encoded_stored)?;
        let encoded_record = self.load_stored_logical_outbox(tenant_id, owner_user_id, &stored)?;
        let record = SealedLogicalOutboxRecord::decode(&encoded_record)?;
        if record.event_id != event_id
            || record.identity.sequence != sequence
            || record.identity.tenant_id != tenant_id
            || record.identity.owner_user_id != owner_user_id
        {
            return Err(invalid_data(
                "logical outbox acknowledgement identity differs",
            ));
        }
        let next_pending_bytes = status
            .pending_bytes
            .checked_sub(encoded_record.len() as u64)
            .ok_or_else(|| invalid_data("logical outbox pending byte count underflow"))?;
        self.entities.delete(&mut transaction.entity, record_key)?;
        self.put_outbox_counter(&mut transaction, OUTBOX_PUBLISHED_SEQUENCE_KEY, sequence)?;
        self.put_outbox_counter(
            &mut transaction,
            OUTBOX_PENDING_BYTES_KEY,
            next_pending_bytes,
        )?;
        transaction.internal_maintenance = true;
        self.commit(transaction)?;
        Ok(LogicalOutboxStatus {
            last_sequence: status.last_sequence,
            published_sequence: sequence,
            pending_bytes: next_pending_bytes,
        })
    }

    pub(crate) fn acknowledge_search_dirty(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        tokens: &[crate::search_dirty::DirtyToken],
    ) -> io::Result<usize> {
        use crate::generated_tofudb_ir::{
            CONVERSATION_SEARCH_DIRTY_NAMESPACE, TURN_SEARCH_DIRTY_NAMESPACE,
        };

        if tokens.is_empty() || tokens.len() > 2 {
            return Err(invalid_input("invalid search dirty acknowledgement batch"));
        }
        let mut transaction = self.begin(tenant_id, owner_user_id)?;
        let mut acknowledged = 0usize;
        for token in tokens {
            if token.key.tenant_id() != tenant_id
                || token.key.owner_user_id() != owner_user_id
                || !matches!(
                    token.key.namespace(),
                    TURN_SEARCH_DIRTY_NAMESPACE | CONVERSATION_SEARCH_DIRTY_NAMESPACE
                )
                || token.value.len() != 8
            {
                return Err(invalid_input(
                    "search dirty acknowledgement escaped its scope",
                ));
            }
            if self.entities.get(&mut transaction.entity, &token.key)? == Some(token.value.clone())
            {
                self.entities
                    .delete(&mut transaction.entity, token.key.clone())?;
                acknowledged += 1;
            }
        }
        if acknowledged != 0 {
            transaction.internal_maintenance = true;
            self.commit(transaction)?;
        }
        Ok(acknowledged)
    }

    pub fn receipt_lookup(
        &self,
        transaction: &mut AuthorityTransaction,
        command_id: &str,
        operation: &str,
        request_digest: [u8; 32],
    ) -> io::Result<Option<Vec<u8>>> {
        let key = command_receipt_entity_key(
            transaction.tenant_id,
            transaction.owner_user_id,
            command_id,
        )?;
        let Some(encoded_entry) = self.entity_get(transaction, &key)? else {
            return Ok(None);
        };
        let receipt = CommandReceipt::decode(&encoded_entry)?;
        if receipt.operation != operation || receipt.request_digest != request_digest {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "command ID was reused for a different request",
            ));
        }
        let encoded_response = match receipt.response {
            StoredReceiptResponse::Inline(response) => response,
            StoredReceiptResponse::Blob(reference) => {
                let mut reader = self.reachable_blob_reader(
                    transaction.tenant_id,
                    transaction.owner_user_id,
                    reference,
                )?;
                let mut response = Vec::with_capacity(reference.logical_bytes as usize);
                while let Some(chunk) = reader.next_chunk()? {
                    response.extend_from_slice(&chunk);
                    if response.len() > MAX_STORED_RECEIPT_BYTES {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "receipt blob exceeds its stored byte budget",
                        ));
                    }
                }
                response
            }
        };
        Ok(Some(decode_receipt_response(&encoded_response)?))
    }

    pub fn receipt_insert(
        &self,
        transaction: &mut AuthorityTransaction,
        command_id: &str,
        operation: &str,
        request_digest: [u8; 32],
        raw_response: &[u8],
        committed_at_ms: u64,
    ) -> io::Result<()> {
        if transaction.receipt_record.is_some() {
            return Err(invalid_input(
                "authority transaction contains more than one command receipt",
            ));
        }
        validate_receipt_identity(operation, committed_at_ms)?;
        let command_key = command_receipt_key(command_id)?;
        let entity_key = command_receipt_entity_key(
            transaction.tenant_id,
            transaction.owner_user_id,
            command_id,
        )?;
        if self.entity_get(transaction, &entity_key)?.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "command receipt already exists",
            ));
        }
        let encoded_response = encode_receipt_response(raw_response)?;
        let response = if encoded_response.len() <= MAX_INLINE_RECEIPT_BYTES {
            StoredReceiptResponse::Inline(encoded_response)
        } else {
            let maximum_bytes = encoded_response.len() as u64;
            let reference =
                self.stage_blob(transaction, &mut encoded_response.as_slice(), maximum_bytes)?;
            StoredReceiptResponse::Blob(reference)
        };
        let entry = CommandReceipt {
            operation: operation.to_owned(),
            request_digest,
            committed_at_ms,
            response,
        }
        .encode()?;
        self.entity_put(transaction, entity_key, entry.clone())?;
        transaction.receipt_record = Some(encode_receipt_family_record(
            transaction.tenant_id,
            transaction.owner_user_id,
            command_key,
            &entry,
        )?);
        Ok(())
    }

    pub fn commit(
        &mut self,
        transaction: AuthorityTransaction,
    ) -> io::Result<AuthorityCommitResult> {
        if !EntityDatabase::transaction_is_writable(&transaction.entity) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        let AuthorityTransaction {
            authority_uuid,
            mut entity,
            stream_appends,
            staged_blob_block_ids,
            receipt_record,
            logical_outbox_record,
            has_business_mutation,
            content_free_diagnostic_outbox_exemption,
            internal_maintenance,
            ..
        } = transaction;
        if authority_uuid != self.entities.engine().state().authority_uuid {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "authority transaction belongs to a different database",
            ));
        }
        if internal_maintenance
            && (has_business_mutation
                || logical_outbox_record.is_some()
                || receipt_record.is_some()
                || !stream_appends.is_empty())
        {
            return Err(invalid_input(
                "internal maintenance cannot contain business mutations",
            ));
        }
        if logical_outbox_record.is_some() && !has_business_mutation {
            return Err(invalid_input(
                "logical outbox record requires a business mutation",
            ));
        }
        if content_free_diagnostic_outbox_exemption && logical_outbox_record.is_some() {
            return Err(invalid_input(
                "diagnostic outbox exemption cannot accompany outbox capture",
            ));
        }
        if self.logical_outbox.is_some()
            && has_business_mutation
            && logical_outbox_record.is_none()
            && !content_free_diagnostic_outbox_exemption
        {
            return Err(invalid_input(
                "configured logical outbox requires atomic capture",
            ));
        }
        let mut prepared_streams: Vec<PreparedStreamAppend> = Vec::new();
        for append in stream_appends {
            let committed_event_count =
                persisted_event_count(&self.entities, &mut entity, &append.key)?;
            let prepared = prepare_persisted_append(
                self.entities.engine(),
                &append.key,
                committed_event_count,
                append.expected_next_sequence,
                &append.events,
            )?;
            prepared.stage_persisted_catalog(&self.entities, &mut entity)?;
            prepared_streams.push(prepared);
        }
        let prepared_entity = self.entities.prepare_commit(entity)?;

        let mut builder = FamilyTransactionBuilder::default();
        if let Some(root_record) = prepared_entity.root_record() {
            builder.add_record(
                FamilyRecordKind::EntityRoot,
                root_record.to_vec(),
                prepared_entity.block_ids().iter().copied(),
            )?;
        }
        for prepared in &prepared_streams {
            builder.add_record(
                FamilyRecordKind::StreamCommit,
                prepared.commit_record().to_vec(),
                prepared.block_ids().iter().copied(),
            )?;
        }
        if let Some(receipt_record) = receipt_record {
            builder.add_record(
                FamilyRecordKind::CommandReceipt,
                receipt_record,
                std::iter::empty(),
            )?;
        }
        if let Some(logical_outbox_record) = logical_outbox_record {
            builder.add_record(
                FamilyRecordKind::LogicalOutbox,
                logical_outbox_record,
                std::iter::empty(),
            )?;
        }
        builder.add_block_references(staged_blob_block_ids)?;
        if prepared_entity.root_record().is_none() && prepared_streams.is_empty() {
            return Err(invalid_input(
                "authority transaction has no durable semantic mutation",
            ));
        }
        let envelope = builder.prepare()?;
        let committed = self
            .entities
            .engine_mut()
            .commit_references_with_authority_state(
                &envelope.inline_payload,
                &envelope.block_ids,
                prepared_entity.authority_state_root(),
            )?;
        self.entities
            .apply_prepared_commit(prepared_entity, committed.sequence)?;
        let stream_results = prepared_streams
            .iter()
            .map(|prepared| prepared.committed_result(committed.sequence))
            .collect();
        Ok(AuthorityCommitResult {
            transaction_sequence: committed.sequence,
            stream_appends: stream_results,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::logical_outbox::decode_logical_outbox_family_record;
    use crate::transaction::decode_family_records;
    use crate::vfs::{DeterministicVfs, FaultAction, FaultRule, OpenRequest, Operation, Vfs};

    fn entity_key(name: &[u8]) -> EntityKey {
        EntityKey::new(7, 11, "semantic", name).unwrap()
    }

    fn stream_key(name: &[u8]) -> StreamKey {
        StreamKey::new(7, 11, "semantic", name).unwrap()
    }

    fn event(value: u8) -> StreamEvent {
        StreamEvent::new(value as i64, "updated", vec![value; 32]).unwrap()
    }

    fn outbox_capture(payload: Vec<u8>, command_id: Option<&str>) -> LogicalOutboxCapture {
        LogicalOutboxCapture {
            schema_version: 57,
            registry_version: 37,
            operation: "artifact.create".to_owned(),
            request_id: "request-1".to_owned(),
            request_digest: [6; 32],
            command_id: command_id.map(str::to_owned),
            committed_at_ms: 4_000,
            clear_payload: payload,
        }
    }

    fn prepared_simulated_vfs() -> Arc<DeterministicVfs> {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        vfs.arm_fault(None).unwrap();
        vfs
    }

    fn commit_simulated_outbox(database: &mut AuthorityDatabase) -> io::Result<()> {
        database.configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)?;
        let mut transaction = database.begin(7, 11)?;
        database.entity_put(
            &mut transaction,
            entity_key(b"artifact"),
            b"created".to_vec(),
        )?;
        database.logical_outbox_capture(
            &mut transaction,
            outbox_capture(b"transaction IR".to_vec(), Some("command-1")),
        )?;
        database.commit(transaction)?;
        Ok(())
    }

    fn assert_recovered_outbox_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let sequence = recovered.entities.engine().state().durable_sequence;
        assert!(sequence <= 1);
        let mut inspect = recovered.begin(7, 11).unwrap();
        let entity = recovered
            .entity_get(&mut inspect, &entity_key(b"artifact"))
            .unwrap();
        let pending = recovered.logical_outbox_pending(7, 11, 1).unwrap();
        match sequence {
            0 => {
                assert_eq!(entity, None);
                assert!(pending.is_empty());
            }
            1 => {
                assert_eq!(entity, Some(b"created".to_vec()));
                assert_eq!(pending.len(), 1);
                assert_eq!(pending[0].record.identity.sequence, 1);
            }
            _ => unreachable!(),
        }
    }

    fn simulated_database_with_pending_outbox(
        vfs: Arc<DeterministicVfs>,
    ) -> (AuthorityDatabase, [u8; 32]) {
        let mut database =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        vfs.arm_fault(None).unwrap();
        commit_simulated_outbox(&mut database).unwrap();
        let event_id = database.logical_outbox_pending(7, 11, 1).unwrap()[0]
            .record
            .event_id;
        vfs.arm_fault(None).unwrap();
        (database, event_id)
    }

    fn assert_recovered_ack_prefix(vfs: Arc<DeterministicVfs>) {
        vfs.crash().unwrap();
        vfs.arm_fault(None).unwrap();
        let mut recovered = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        recovered
            .configure_logical_outbox(&[31; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let durable_sequence = recovered.entities.engine().state().durable_sequence;
        assert!((1..=2).contains(&durable_sequence));
        let status = recovered.logical_outbox_status(7, 11).unwrap();
        let pending = recovered.logical_outbox_pending(7, 11, 1).unwrap();
        if durable_sequence == 1 {
            assert_eq!(status.published_sequence, 0);
            assert!(status.pending_bytes > 0);
            assert_eq!(pending.len(), 1);
        } else {
            assert_eq!(status.published_sequence, 1);
            assert_eq!(status.pending_bytes, 0);
            assert!(pending.is_empty());
        }
    }

    #[test]
    fn entity_stream_and_blob_publish_in_one_recoverable_transaction() {
        let directory = tempfile::tempdir().unwrap();
        let blob_payload = vec![23_u8; 1024 * 1024 + 17];
        let blob_reference;
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            blob_reference = database
                .stage_blob(
                    &mut transaction,
                    &mut blob_payload.as_slice(),
                    blob_payload.len() as u64,
                )
                .unwrap();
            database
                .entity_put(&mut transaction, entity_key(b"blob"), b"present".to_vec())
                .unwrap();
            database
                .stream_append(&mut transaction, stream_key(b"events"), 1, vec![event(1)])
                .unwrap();
            let committed = database.commit(transaction).unwrap();
            assert_eq!(committed.transaction_sequence, 1);
            assert_eq!(committed.stream_appends.len(), 1);
            assert_eq!(committed.stream_appends[0].transaction_sequence, 1);
        }

        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .entity_get(&mut transaction, &entity_key(b"blob"))
                .unwrap(),
            Some(b"present".to_vec())
        );
        let page = database
            .stream_read(7, 11, &stream_key(b"events"), 1, 10)
            .unwrap();
        assert_eq!(page.events.len(), 1);
        let mut restored_blob = Vec::new();
        let mut reader = database.blob_reader(7, 11, blob_reference).unwrap();
        while let Some(chunk) = reader.next_chunk().unwrap() {
            restored_blob.extend_from_slice(&chunk);
        }
        assert_eq!(restored_blob, blob_payload);
    }

    #[test]
    fn authority_open_uses_control_root_without_reading_latest_history() {
        let vfs = prepared_simulated_vfs();
        let key = stream_key(b"events");
        let latest_history;
        {
            let mut database =
                AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            let mut first = database.begin(7, 11).unwrap();
            database
                .stream_append(&mut first, key.clone(), 1, vec![event(1)])
                .unwrap();
            database.commit(first).unwrap();
            database.entities.engine_mut().checkpoint().unwrap();
            let mut second = database.begin(7, 11).unwrap();
            database
                .stream_append(&mut second, key.clone(), 2, vec![event(2)])
                .unwrap();
            database.commit(second).unwrap();
            database.entities.engine_mut().checkpoint().unwrap();
            assert_eq!(
                database.entities.engine().history_segment_block_ids().len(),
                2
            );
            latest_history = database.entities.engine().history_segment_block_ids()[1];
            assert_eq!(
                database.entities.engine().state().authority_state_root,
                Some(database.entities.snapshot().root)
            );
        }

        let hexadecimal = latest_history.to_hex();
        let path = Path::new("/data")
            .join("blocks")
            .join(&hexadecimal[..2])
            .join(format!("{hexadecimal}.blk"));
        let mut file = vfs
            .open(
                &path,
                OpenRequest {
                    write: true,
                    ..OpenRequest::default()
                },
            )
            .unwrap();
        file.write_all_at(16, b"X").unwrap();
        file.sync_all().unwrap();

        let database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        assert_eq!(database.stream_next_sequence(7, 11, &key).unwrap(), 3);
        let page = database.stream_read(7, 11, &key, 1, 10).unwrap();
        assert_eq!(
            page.events
                .into_iter()
                .map(|event| (event.sequence, event.event))
                .collect::<Vec<_>>(),
            vec![(1, event(1)), (2, event(2))]
        );
    }

    #[test]
    fn persisted_stream_reader_seeks_and_selects_across_segments() {
        let directory = tempfile::tempdir().unwrap();
        let key = stream_key(b"segmented-events");
        let expected = (1_u8..=10)
            .map(|value| StreamEvent::new(value as i64, "large", vec![value; 250 * 1024]).unwrap())
            .collect::<Vec<_>>();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut append = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut append, key.clone(), 1, expected.clone())
            .unwrap();
        let committed = database.commit(append).unwrap();
        assert!(committed.stream_appends[0].segment_count > 1);

        let page = database.stream_read(7, 11, &key, 7, 3).unwrap();
        assert_eq!(
            page.events
                .iter()
                .map(|event| (event.sequence, event.event.created_at_ms))
                .collect::<Vec<_>>(),
            vec![(7, 7), (8, 8), (9, 9)]
        );
        assert_eq!(page.next_sequence, 10);
        assert!(!page.end_of_stream);

        let positions = BTreeSet::from([1, 8, 10]);
        let selected = database
            .stream_read_positions(7, 11, &key, &positions)
            .unwrap();
        assert_eq!(selected.get(&1), Some(&expected[0]));
        assert_eq!(selected.get(&8), Some(&expected[7]));
        assert_eq!(selected.get(&10), Some(&expected[9]));
    }

    #[test]
    fn backup_restore_preserves_control_authority_root() {
        let source = tempfile::tempdir().unwrap();
        let backup = tempfile::tempdir().unwrap();
        let target = tempfile::tempdir().unwrap();
        let key = stream_key(b"backup-events");
        let mut database = AuthorityDatabase::initialize(source.path()).unwrap();
        let mut append = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut append, key.clone(), 1, vec![event(1), event(2)])
            .unwrap();
        database.commit(append).unwrap();
        crate::backup::create_incremental_backup(database.entities.engine_mut(), backup.path())
            .unwrap();
        let expected_root = database.entities.engine().state().authority_state_root;
        drop(database);

        let restored_manifest =
            crate::backup::restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored_manifest.authority_state_root, expected_root);
        let restored = AuthorityDatabase::open(target.path()).unwrap();
        assert_eq!(
            restored.entities.engine().state().authority_state_root,
            expected_root
        );
        assert_eq!(
            restored
                .stream_read(7, 11, &key, 1, 10)
                .unwrap()
                .events
                .len(),
            2
        );
    }

    #[test]
    fn range_mount_consolidation_commits_as_physical_maintenance() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let key = EntityKey::new(7, 11, "conversation", b"one").unwrap();
        let end = EntityKey::new(7, 11, "conversation", b"one\0").unwrap();
        let mut seed = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut seed, key.clone(), b"value".to_vec())
            .unwrap();
        database.commit(seed).unwrap();
        let mut retire = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_pin(
                &mut retire,
                b"maintenance",
                &[(key.clone(), end.clone())],
            )
            .unwrap();
        database
            .entity_retire_range(&mut retire, &key, &end)
            .unwrap();
        database.commit(retire).unwrap();
        let mut restore = database.begin(7, 11).unwrap();
        database
            .stage_persistent_range_snapshot_restore(
                &mut restore,
                b"maintenance",
                &[(key.clone(), end)],
            )
            .unwrap();
        database.commit(restore).unwrap();

        let mut maintenance = database.begin(7, 11).unwrap();
        let progress = database
            .consolidate_one_entity_range_mount(&mut maintenance)
            .unwrap()
            .unwrap();
        assert!(progress.mount_completed);
        assert_eq!(progress.rows_materialized, 1);
        database.commit(maintenance).unwrap();

        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(
            database.entity_get(&mut read, &key).unwrap(),
            Some(b"value".to_vec())
        );
    }

    #[test]
    fn entity_only_commits_do_not_disturb_persisted_stream_catalog() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();

        let mut first = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut first, stream_key(b"events"), 1, vec![event(1)])
            .unwrap();
        assert_eq!(database.commit(first).unwrap().transaction_sequence, 1);

        let mut entity_only = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut entity_only, entity_key(b"state"), b"ready".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(entity_only).unwrap().transaction_sequence,
            2
        );

        let mut second = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut second, stream_key(b"events"), 2, vec![event(2)])
            .unwrap();
        assert_eq!(database.commit(second).unwrap().transaction_sequence, 3);
        let page = database
            .stream_read(7, 11, &stream_key(b"events"), 1, 10)
            .unwrap();
        assert_eq!(page.events.len(), 2);
    }

    #[test]
    fn persisted_stream_cursor_is_atomic_and_corruption_fails_closed() {
        let directory = tempfile::tempdir().unwrap();
        let key = stream_key(b"persisted-events");
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut append = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut append, key.clone(), 1, vec![event(1), event(2)])
            .unwrap();
        database.commit(append).unwrap();
        assert_eq!(database.stream_next_sequence(7, 11, &key).unwrap(), 3);
        drop(database);

        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        assert_eq!(database.stream_next_sequence(7, 11, &key).unwrap(), 3);
        let mut corrupt = database.entities.begin(7, 11).unwrap();
        database
            .entities
            .put(
                &mut corrupt,
                crate::stream::catalog_metadata_key(&key).unwrap(),
                b"not-stream-metadata".to_vec(),
            )
            .unwrap();
        database.entities.commit(corrupt).unwrap();
        assert_eq!(
            database
                .stream_next_sequence(7, 11, &key)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
    }

    #[test]
    fn transaction_merges_contiguous_stream_appends_and_rejects_gaps_and_empty_work() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut transaction, stream_key(b"events"), 1, vec![event(1)])
            .unwrap();
        database
            .stream_append(&mut transaction, stream_key(b"events"), 2, vec![event(2)])
            .unwrap();
        assert_eq!(
            database
                .stream_append(&mut transaction, stream_key(b"events"), 4, vec![event(4)])
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(
            database.commit(transaction).unwrap().transaction_sequence,
            1
        );
        let page = database
            .stream_read(7, 11, &stream_key(b"events"), 1, 10)
            .unwrap();
        assert_eq!(
            page.events
                .iter()
                .map(|event| (event.sequence, event.event.clone()))
                .collect::<Vec<_>>(),
            vec![(1, event(1)), (2, event(2))]
        );
        let empty = database.begin(7, 11).unwrap();
        assert_eq!(
            database.commit(empty).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn transaction_capability_cannot_cross_authority_uuid() {
        let left_directory = tempfile::tempdir().unwrap();
        let right_directory = tempfile::tempdir().unwrap();
        let left = AuthorityDatabase::initialize(left_directory.path()).unwrap();
        let mut right = AuthorityDatabase::initialize(right_directory.path()).unwrap();
        let mut transaction = left.begin(7, 11).unwrap();
        left.stream_append(&mut transaction, stream_key(b"events"), 1, vec![event(1)])
            .unwrap();
        assert_eq!(
            right.commit(transaction).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn receipt_replays_after_reopen_and_rejects_command_reuse() {
        let directory = tempfile::tempdir().unwrap();
        let response = br#"{"created":true,"id":"artifact-1"}"#;
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            assert_eq!(
                database
                    .receipt_lookup(&mut transaction, "command-1", "artifact.create", [3; 32])
                    .unwrap(),
                None
            );
            database
                .entity_put(
                    &mut transaction,
                    entity_key(b"artifact-1"),
                    b"created".to_vec(),
                )
                .unwrap();
            database
                .receipt_insert(
                    &mut transaction,
                    "command-1",
                    "artifact.create",
                    [3; 32],
                    response,
                    1_000,
                )
                .unwrap();
            assert_eq!(
                database.commit(transaction).unwrap().transaction_sequence,
                1
            );
            let committed = database
                .entities
                .engine()
                .committed_transactions()
                .last()
                .unwrap();
            let records = decode_family_records(&committed.envelope.inline_payload)
                .unwrap()
                .unwrap();
            assert!(records
                .iter()
                .any(|record| record.kind == FamilyRecordKind::CommandReceipt));
        }

        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut replay = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .receipt_lookup(&mut replay, "command-1", "artifact.create", [3; 32])
                .unwrap(),
            Some(response.to_vec())
        );
        let mut conflict = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .receipt_lookup(&mut conflict, "command-1", "artifact.create", [4; 32])
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[test]
    fn large_receipt_uses_an_atomically_referenced_blob() {
        let directory = tempfile::tempdir().unwrap();
        let mut state = 0x1234_5678_u32;
        let response = (0..10_000)
            .map(|_| {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                state as u8
            })
            .collect::<Vec<_>>();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .receipt_insert(
                    &mut transaction,
                    "command-large",
                    "artifact.create",
                    [8; 32],
                    &response,
                    2_000,
                )
                .unwrap();
            database.commit(transaction).unwrap();
            database.entities.engine_mut().checkpoint().unwrap();
            for (name, value) in [
                (b"later-1".as_slice(), b"one".as_slice()),
                (b"later-2", b"two"),
            ] {
                let mut later = database.begin(7, 11).unwrap();
                database
                    .entity_put(&mut later, entity_key(name), value.to_vec())
                    .unwrap();
                database.commit(later).unwrap();
                database.entities.engine_mut().checkpoint().unwrap();
            }
            let metrics = database.compact_history(1).unwrap();
            assert_eq!(metrics.retired_segments, 2);
            assert_eq!(metrics.retained_first_sequence, 3);
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut inspect = database.begin(7, 11).unwrap();
        let stored = database
            .entity_get(
                &mut inspect,
                &command_receipt_entity_key(7, 11, "command-large").unwrap(),
            )
            .unwrap()
            .unwrap();
        assert!(matches!(
            CommandReceipt::decode(&stored).unwrap().response,
            StoredReceiptResponse::Blob(_)
        ));
        let mut replay = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .receipt_lookup(&mut replay, "command-large", "artifact.create", [8; 32],)
                .unwrap(),
            Some(response)
        );
    }

    #[test]
    fn concurrent_first_delivery_commits_business_mutation_only_once() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut first = database.begin(7, 11).unwrap();
        let mut second = database.begin(7, 11).unwrap();
        for transaction in [&mut first, &mut second] {
            assert!(database
                .receipt_lookup(transaction, "same-command", "artifact.create", [5; 32],)
                .unwrap()
                .is_none());
            database
                .entity_put(transaction, entity_key(b"artifact"), b"created".to_vec())
                .unwrap();
            database
                .receipt_insert(
                    transaction,
                    "same-command",
                    "artifact.create",
                    [5; 32],
                    br#"{"created":true}"#,
                    3_000,
                )
                .unwrap();
        }
        assert_eq!(database.commit(first).unwrap().transaction_sequence, 1);
        assert_eq!(
            database.commit(second).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(database.entities.engine().state().durable_sequence, 1);
    }

    #[test]
    fn logical_outbox_is_atomic_recoverable_and_strictly_acknowledged() {
        let directory = tempfile::tempdir().unwrap();
        let key = [17; 32];
        let clear_payload = b"canonical transaction IR".to_vec();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            database
                .configure_logical_outbox(&key, 1024 * 1024, 128 * 1024)
                .unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(
                    &mut transaction,
                    entity_key(b"artifact"),
                    b"created".to_vec(),
                )
                .unwrap();
            assert_eq!(
                database
                    .logical_outbox_capture(
                        &mut transaction,
                        outbox_capture(clear_payload.clone(), Some("command-1")),
                    )
                    .unwrap(),
                1
            );
            assert_eq!(
                database.commit(transaction).unwrap().transaction_sequence,
                1
            );
            let records = decode_family_records(
                &database
                    .entities
                    .engine()
                    .committed_transactions()
                    .last()
                    .unwrap()
                    .envelope
                    .inline_payload,
            )
            .unwrap()
            .unwrap();
            let family = records
                .iter()
                .find(|record| record.kind == FamilyRecordKind::LogicalOutbox)
                .unwrap();
            let family = decode_logical_outbox_family_record(family.payload).unwrap();
            assert_eq!(
                (family.tenant_id, family.owner_user_id, family.sequence),
                (7, 11, 1)
            );
            assert!(matches!(
                family.stored,
                StoredLogicalOutboxRecord::Inline(_)
            ));
        }

        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        database
            .configure_logical_outbox(&key, 1024 * 1024, 128 * 1024)
            .unwrap();
        let pending = database.logical_outbox_pending(7, 11, 64).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(
            database.logical_outbox_decrypt(&pending[0].record).unwrap(),
            clear_payload
        );
        let event_id = pending[0].record.event_id;
        assert_eq!(
            database.logical_outbox_status(7, 11).unwrap(),
            LogicalOutboxStatus {
                last_sequence: 1,
                published_sequence: 0,
                pending_bytes: pending[0].record_bytes,
            }
        );
        assert_eq!(
            database
                .logical_outbox_acknowledge(7, 11, 1, [9; 32])
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        let acknowledged = database
            .logical_outbox_acknowledge(7, 11, 1, event_id)
            .unwrap();
        assert_eq!(acknowledged.published_sequence, 1);
        assert_eq!(acknowledged.pending_bytes, 0);
        assert!(database
            .logical_outbox_pending(7, 11, 1)
            .unwrap()
            .is_empty());
        assert_eq!(
            database
                .logical_outbox_acknowledge(7, 11, 1, event_id)
                .unwrap(),
            acknowledged
        );
        drop(database);

        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&key, 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(reopened.logical_outbox_status(7, 11).unwrap(), acknowledged);
    }

    #[test]
    fn large_outbox_uses_blob_and_pending_budget_backpressures_before_commit() {
        let directory = tempfile::tempdir().unwrap();
        let key = [18; 32];
        let payload = (0..10_000).map(|value| value as u8).collect::<Vec<_>>();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&key, 64 * 1024, 32 * 1024)
            .unwrap();
        let mut first = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut first, entity_key(b"first"), b"created".to_vec())
            .unwrap();
        database
            .logical_outbox_capture(&mut first, outbox_capture(payload.clone(), None))
            .unwrap();
        database.commit(first).unwrap();
        let committed = database
            .entities
            .engine()
            .committed_transactions()
            .last()
            .unwrap();
        let records = decode_family_records(&committed.envelope.inline_payload)
            .unwrap()
            .unwrap();
        let family = records
            .iter()
            .find(|record| record.kind == FamilyRecordKind::LogicalOutbox)
            .unwrap();
        assert!(matches!(
            decode_logical_outbox_family_record(family.payload)
                .unwrap()
                .stored,
            StoredLogicalOutboxRecord::Blob(_)
        ));
        let pending_bytes = database.logical_outbox_status(7, 11).unwrap().pending_bytes;
        database
            .configure_logical_outbox(&key, pending_bytes, pending_bytes as usize)
            .unwrap();
        let mut rejected = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut rejected, entity_key(b"second"), b"created".to_vec())
            .unwrap();
        assert_eq!(
            database
                .logical_outbox_capture(&mut rejected, outbox_capture(payload, None))
                .unwrap_err()
                .kind(),
            io::ErrorKind::WouldBlock
        );
        assert_eq!(database.entities.engine().state().durable_sequence, 1);
        let mut inspect = database.begin(7, 11).unwrap();
        assert_eq!(
            database
                .entity_get(&mut inspect, &entity_key(b"second"))
                .unwrap(),
            None
        );
    }

    #[test]
    fn configured_outbox_fails_closed_and_concurrent_sequences_do_not_fork() {
        let directory = tempfile::tempdir().unwrap();
        let key = [19; 32];
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&key, 1024 * 1024, 128 * 1024)
            .unwrap();

        let mut missing = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut missing, entity_key(b"missing"), b"created".to_vec())
            .unwrap();
        assert_eq!(
            database.commit(missing).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let mut outbox_only = database.begin(7, 11).unwrap();
        database
            .logical_outbox_capture(&mut outbox_only, outbox_capture(b"payload".to_vec(), None))
            .unwrap();
        assert_eq!(
            database.commit(outbox_only).unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );

        let mut first = database.begin(7, 11).unwrap();
        let mut second = database.begin(7, 11).unwrap();
        for (transaction, name) in [(&mut first, b"first".as_slice()), (&mut second, b"second")] {
            database
                .entity_put(transaction, entity_key(name), b"created".to_vec())
                .unwrap();
            assert_eq!(
                database
                    .logical_outbox_capture(transaction, outbox_capture(name.to_vec(), None),)
                    .unwrap(),
                1
            );
        }
        database.commit(first).unwrap();
        assert_eq!(
            database.commit(second).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        let pending = database.logical_outbox_pending(7, 11, 10).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].record.identity.sequence, 1);
    }

    #[test]
    fn reopened_outbox_rejects_a_different_encryption_key() {
        let directory = tempfile::tempdir().unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            database
                .configure_logical_outbox(&[20; 32], 1024 * 1024, 128 * 1024)
                .unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(
                    &mut transaction,
                    entity_key(b"artifact"),
                    b"created".to_vec(),
                )
                .unwrap();
            database
                .logical_outbox_capture(&mut transaction, outbox_capture(b"payload".to_vec(), None))
                .unwrap();
            database.commit(transaction).unwrap();
        }
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[21; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap_err().kind(),
            io::ErrorKind::PermissionDenied
        );
    }

    #[test]
    fn outbox_sequences_and_pending_cursors_are_owner_scoped() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[22; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        for (owner_user_id, name) in [(11, b"first".as_slice()), (11, b"second"), (12, b"other")] {
            let mut transaction = database.begin(7, owner_user_id).unwrap();
            database
                .entity_put(
                    &mut transaction,
                    EntityKey::new(7, owner_user_id, "semantic", name).unwrap(),
                    b"created".to_vec(),
                )
                .unwrap();
            let sequence = database
                .logical_outbox_capture(
                    &mut transaction,
                    LogicalOutboxCapture {
                        request_id: format!("request-{}", String::from_utf8_lossy(name)),
                        ..outbox_capture(name.to_vec(), None)
                    },
                )
                .unwrap();
            assert_eq!(
                sequence,
                if owner_user_id == 11 && name == b"second" {
                    2
                } else {
                    1
                }
            );
            database.commit(transaction).unwrap();
        }
        assert_eq!(database.logical_outbox_pending(7, 11, 10).unwrap().len(), 2);
        assert_eq!(database.logical_outbox_pending(7, 12, 10).unwrap().len(), 1);
        let second = database.logical_outbox_pending(7, 11, 10).unwrap()[1]
            .record
            .clone();
        assert_eq!(
            database
                .logical_outbox_acknowledge(7, 11, 2, second.event_id)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(
            database
                .logical_outbox_status(7, 12)
                .unwrap()
                .published_sequence,
            0
        );
    }

    #[test]
    fn pending_fetch_never_materializes_more_than_eight_mib() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_logical_outbox(&[23; 32], 16 * 1024 * 1024, 4 * 1024 * 1024)
            .unwrap();
        for index in 0..3_u8 {
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(&mut transaction, entity_key(&[index]), b"created".to_vec())
                .unwrap();
            database
                .logical_outbox_capture(
                    &mut transaction,
                    LogicalOutboxCapture {
                        request_id: format!("large-request-{index}"),
                        request_digest: [index; 32],
                        clear_payload: vec![index; 3 * 1024 * 1024],
                        ..outbox_capture(b"unused".to_vec(), None)
                    },
                )
                .unwrap();
            database.commit(transaction).unwrap();
        }
        let pending = database.logical_outbox_pending(7, 11, 64).unwrap();
        assert_eq!(pending.len(), 2);
        assert!(
            pending
                .iter()
                .map(|record| record.record_bytes)
                .sum::<u64>()
                <= MAX_LOGICAL_OUTBOX_FETCH_BYTES as u64
        );
        assert_eq!(pending[0].record.identity.sequence, 1);
        assert_eq!(pending[1].record.identity.sequence, 2);
    }

    #[test]
    fn every_outbox_commit_io_fault_recovers_only_an_atomic_prefix() {
        let baseline_vfs = prepared_simulated_vfs();
        let mut baseline =
            AuthorityDatabase::initialize_with_vfs(Path::new("/data"), baseline_vfs.clone())
                .unwrap();
        baseline_vfs.arm_fault(None).unwrap();
        commit_simulated_outbox(&mut baseline).unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_simulated_vfs();
            let mut database =
                AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = commit_simulated_outbox(&mut database);
            drop(database);
            assert_recovered_outbox_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_simulated_vfs();
            let mut database =
                AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = commit_simulated_outbox(&mut database);
            drop(database);
            assert_recovered_outbox_prefix(vfs);
        }
    }

    #[test]
    fn every_outbox_ack_io_fault_recovers_only_an_atomic_prefix() {
        let baseline_vfs = prepared_simulated_vfs();
        let (mut baseline, event_id) = simulated_database_with_pending_outbox(baseline_vfs.clone());
        baseline
            .logical_outbox_acknowledge(7, 11, 1, event_id)
            .unwrap();
        let trace = baseline_vfs.trace().unwrap();
        drop(baseline);

        for operation_number in 1..=trace.len() as u64 {
            let vfs = prepared_simulated_vfs();
            let (mut database, event_id) = simulated_database_with_pending_outbox(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number,
                action: FaultAction::ErrorBefore(io::ErrorKind::Interrupted),
            }))
            .unwrap();
            let _ = database.logical_outbox_acknowledge(7, 11, 1, event_id);
            drop(database);
            assert_recovered_ack_prefix(vfs);
        }

        for (index, operation) in trace.iter().enumerate() {
            if operation != &Operation::Write {
                continue;
            }
            let vfs = prepared_simulated_vfs();
            let (mut database, event_id) = simulated_database_with_pending_outbox(vfs.clone());
            vfs.arm_fault(Some(FaultRule {
                operation_number: index as u64 + 1,
                action: FaultAction::ShortWrite(7),
            }))
            .unwrap();
            let _ = database.logical_outbox_acknowledge(7, 11, 1, event_id);
            drop(database);
            assert_recovered_ack_prefix(vfs);
        }
    }
}
