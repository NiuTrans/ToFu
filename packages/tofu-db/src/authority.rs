//! One synchronous authority coordinating entity, stream, and blob families.
//!
//! This is the semantic executor's storage boundary. It stages every family
//! before publishing exactly one WAL transaction, then advances all in-memory
//! projections to that durable sequence. Pinned replicas prepare certified
//! transactions independently; the writer validates and publishes their effects.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Read};
use std::path::Path;
use std::sync::Arc;
use uuid::Uuid;

#[path = "authority_query_view.rs"]
mod query_view;
pub(crate) use query_view::PublishedQueryView;
pub use query_view::PublishedQueryObserver;

use crate::authority_gc::{
    AuthorityGarbageCollectionBudget, AuthorityGarbageCollectionMetrics,
    AuthorityGarbageCollectionMode, AuthorityGarbageCollectionSession,
};
use crate::blob::{BlobReader, BlobReference};
use crate::block::BlockId;
use crate::engine::{Engine, HistoryCompactionMetrics};
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
    INTEGRATION_WORKSPACE_NAMESPACE, KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
    ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE, ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE,
    ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE, ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_CORE_NAMESPACE, ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE, ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE, ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE,
    ORCHESTRATION_RUN_STATE_NAMESPACE, ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE,
    PROVIDER_ID_CLAIM_NAMESPACE, QUEUE_AUTOPILOT_MARKER_NAMESPACE,
    QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE, QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE,
    QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE, QUEUE_ITEM_CORE_NAMESPACE, QUEUE_ITEM_ID_CLAIM_NAMESPACE,
    QUEUE_ITEM_STATE_NAMESPACE, SCHEDULER_POLL_SEQUENCE_NAMESPACE,
    SCHEDULER_TASK_DOCUMENT_NAMESPACE, SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE,
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
    CommandReceipt, ReceiptSnapshot, StoredReceiptResponse, MAX_INLINE_RECEIPT_BYTES,
};
use crate::stream::{
    persisted_event_count, prepare_persisted_append, read_persisted, read_persisted_positions,
    retire_persisted_prefix, PreparedStreamAppend, StreamAppendResult, StreamEvent, StreamKey,
    StreamPage, StreamRetirementProgress,
};
use crate::transaction::{FamilyRecordKind, FamilyTransactionBuilder};
use crate::vfs::Vfs;

pub const MAX_LOGICAL_OUTBOX_FETCH_RECORDS: usize = 64;
pub const MAX_LOGICAL_OUTBOX_FETCH_BYTES: usize = 8 * 1024 * 1024;

/// Headroom under the hard active-log ceiling at which the server layer
/// starts parking appends for the rotation install window.
pub(crate) const ROTATION_PARK_WAL_MARGIN_BYTES: u64 = 1024 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ConversationActivityBackfillProgress {
    pub processed_rows: usize,
    pub source_bytes: usize,
    pub complete: bool,
    pub committed: bool,
    pub durable_sequence: u64,
}

pub use crate::integration_workspace::MAX_LEGACY_CHECKOUT_BACKFILL;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct IntegrationCheckoutBackfillProgress {
    pub processed_rows: usize,
    pub after_id: u64,
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

#[path = "authority_preparation.rs"]
mod preparation;

pub struct AuthorityTransaction {
    prepared_blobs: Option<Arc<preparation::PreparedBlobStore>>,
    conversation_notices: std::collections::BTreeMap<String, u64>,
    authority_uuid: Uuid,
    tenant_id: u64,
    owner_user_id: u64,
    entity: EntityTransaction,
    stream_appends: Vec<PendingStreamAppend>,
    stream_bytes: usize,
    staged_blob_block_ids: BTreeSet<BlockId>,
    receipt_record: Option<Vec<u8>>,
    logical_outbox_record: Option<Vec<u8>>,
    deferred_outbox_capture: Option<LogicalOutboxCapture>,
    deferred_indexes: Vec<crate::conversation_header::recency::DeferredMutation>,
    has_business_mutation: bool,
    content_free_diagnostic_outbox_exemption: bool,
    internal_maintenance: bool,
}

impl AuthorityTransaction {
    /// Coalesce content-free hints while their durable sync rows are staged.
    pub(crate) fn note_conversation_commit(&mut self, key: &str, sequence: u64) -> io::Result<()> {
        use crate::generated_storage_v2::{MAX_COMMIT_NOTICE_KEYS, MAX_COMMIT_NOTICE_KEY_BYTES};
        if key.is_empty() || key.len() > MAX_COMMIT_NOTICE_KEY_BYTES || sequence == 0 {
            return Err(invalid_input("invalid conversation commit notice"));
        }
        if !self.conversation_notices.contains_key(key)
            && self.conversation_notices.len() >= MAX_COMMIT_NOTICE_KEYS
        {
            return Err(io::Error::new(
                io::ErrorKind::OutOfMemory,
                "commit notice budget exceeded",
            ));
        }
        self.conversation_notices
            .entry(key.to_owned())
            .and_modify(|old| *old = (*old).max(sequence))
            .or_insert(sequence);
        Ok(())
    }

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
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ConversationCommitNotice {
    pub user_id: u64,
    pub key: String,
    pub sequence: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct AuthorityCommitResult {
    pub conversation_notices: Vec<ConversationCommitNotice>,
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

#[derive(Clone)]
struct LogicalOutboxRuntime {
    cipher: LogicalOutboxCipher,
}

/// Everything one online-GC epoch needs after its snapshot hold releases
/// (docs/TOFUDB_ONLINE_GC.md): the frozen mark inputs plus shared read
/// paths for the off-mutex mark/scan phases.
pub(crate) struct OnlineGcCapture {
    pub snapshot: crate::engine::OnlineGcMarkSnapshot,
    pub blocks: Arc<crate::block::BlockStore>,
    pub vfs: Arc<dyn crate::vfs::Vfs>,
    pub data_dir: std::path::PathBuf,
}

/// See `AuthorityDatabase::capture_backup_anchor`.
pub(crate) struct BackupAnchorCapture {
    pub manifest: crate::backup::BackupManifest,
    pub anchor_token: u64,
    pub source: Arc<crate::block::BlockStore>,
}

pub use crate::canonical_json_bytes::cache::{CanonicalProofCacheObserver, CanonicalProofCacheMetrics};

pub use crate::blob::decode_cache::{DecodedChunkCacheObserver, DecodedChunkCacheMetrics};

pub struct AuthorityDatabase {
    published_query_options: Option<Arc<std::sync::RwLock<query_view::ReadOptions>>>,
    published_query_results: Option<Arc<crate::query_result_cache::QueryResultCache>>,
    decoded_chunk_cache: Arc<crate::blob::decode_cache::DecodedChunkCache>,
    canonical_json_cache: Arc<crate::canonical_json_bytes::cache::CanonicalProofCache>,
    event_read_epoch: Uuid,
    worker_lease_clock: Arc<crate::worker_lease_clock::WorkerLeaseClock>,
    preparation: Option<Arc<preparation::PreparedBlobStore>>,
    entities: EntityDatabase,
    foreground_admissions: std::sync::Arc<std::sync::atomic::AtomicU64>,
    logical_outbox: Option<LogicalOutboxRuntime>,
    timer_live_capacity: usize,
    online_gc: std::sync::Mutex<
        Option<crate::authority_gc_online::OnlineGarbageCollectionHandle>,
    >,
    backup_anchors: std::sync::Mutex<
        Vec<(u64, crate::online_backup::BackupAnchorProjection)>,
    >,
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
    /// Per-authority progress, observed without taking the contended mutex.
    pub(crate) fn foreground_admissions(&self) -> std::sync::Arc<std::sync::atomic::AtomicU64> {
        std::sync::Arc::clone(&self.foreground_admissions)
    }

    pub(crate) fn note_foreground_admission(&self) {
        self.foreground_admissions.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    }

    pub fn initialize(data_dir: &Path) -> io::Result<Self> {
        let entities = EntityDatabase::initialize(data_dir)?;
        Ok(Self {
            entities,
            event_read_epoch: Uuid::new_v4(),
            worker_lease_clock: Arc::new(crate::worker_lease_clock::WorkerLeaseClock::new()?),
            foreground_admissions: Default::default(),
            canonical_json_cache: Default::default(),
            decoded_chunk_cache: Default::default(),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: None,
            preparation: None,
            timer_live_capacity: 0,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    pub fn open(data_dir: &Path) -> io::Result<Self> {
        let entities = EntityDatabase::open(data_dir)?;
        Ok(Self {
            entities,
            event_read_epoch: Uuid::new_v4(),
            worker_lease_clock: Arc::new(crate::worker_lease_clock::WorkerLeaseClock::new()?),
            foreground_admissions: Default::default(),
            canonical_json_cache: Default::default(),
            decoded_chunk_cache: Default::default(),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: None,
            preparation: None,
            timer_live_capacity: 0,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    /// Opens an authority behind the opt-in at-rest encryption seam, failing
    /// closed on a wrong or absent key. `None` keeps the plaintext real VFS.
    pub fn open_with_key(
        data_dir: &Path,
        key: Option<crate::at_rest::AtRestKey>,
    ) -> io::Result<Self> {
        let engine = crate::engine_log::lifecycle_step("startup_timing", "engine_open",
            || Engine::open_with_key(data_dir, key))?;
        let entities = crate::engine_log::lifecycle_step("startup_timing", "entity_overlay_replay",
            || EntityDatabase::open_engine(engine))?;
        Ok(Self {
            entities,
            event_read_epoch: Uuid::new_v4(),
            worker_lease_clock: Arc::new(crate::worker_lease_clock::WorkerLeaseClock::new()?),
            foreground_admissions: Default::default(),
            canonical_json_cache: Default::default(),
            decoded_chunk_cache: Default::default(),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: None,
            preparation: None,
            timer_live_capacity: 0,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    /// Initializes an authority behind the opt-in at-rest encryption seam.
    pub fn initialize_with_key(
        data_dir: &Path,
        key: Option<crate::at_rest::AtRestKey>,
    ) -> io::Result<Self> {
        let entities =
            EntityDatabase::initialize_with_engine(Engine::initialize_with_key(data_dir, key)?)?;
        Ok(Self {
            entities,
            event_read_epoch: Uuid::new_v4(),
            worker_lease_clock: Arc::new(crate::worker_lease_clock::WorkerLeaseClock::new()?),
            foreground_admissions: Default::default(),
            canonical_json_cache: Default::default(),
            decoded_chunk_cache: Default::default(),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: None,
            preparation: None,
            timer_live_capacity: 0,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    pub fn initialize_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        let entities = EntityDatabase::initialize_with_vfs(data_dir, vfs)?;
        Ok(Self {
            entities,
            event_read_epoch: Uuid::new_v4(),
            worker_lease_clock: Arc::new(crate::worker_lease_clock::WorkerLeaseClock::new()?),
            foreground_admissions: Default::default(),
            canonical_json_cache: Default::default(),
            decoded_chunk_cache: Default::default(),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: None,
            preparation: None,
            timer_live_capacity: 0,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    pub fn open_with_vfs(data_dir: &Path, vfs: Arc<dyn Vfs>) -> io::Result<Self> {
        Self::open_with_vfs_until(data_dir, vfs, &|| false)
    }

    pub(crate) fn open_with_vfs_until(
        data_dir: &Path, vfs: Arc<dyn Vfs>, should_stop: &dyn Fn() -> bool,
    ) -> io::Result<Self> {
        let engine = Engine::open_with_vfs_until(data_dir, vfs, should_stop)?;
        let entities = EntityDatabase::open_engine(engine)?;
        Ok(Self {
            entities,
            event_read_epoch: Uuid::new_v4(),
            worker_lease_clock: Arc::new(crate::worker_lease_clock::WorkerLeaseClock::new()?),
            foreground_admissions: Default::default(),
            canonical_json_cache: Default::default(),
            decoded_chunk_cache: Default::default(),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: None,
            preparation: None,
            timer_live_capacity: 0,
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    /// Volatile source-read lease identity, shared by this authority's replicas.
    pub(crate) fn event_read_epoch(&self) -> Uuid {
        self.event_read_epoch
    }

    /// Worker leases share this backend lifetime with source-read leases.
    pub(crate) fn worker_lease_epoch(&self) -> String {
        self.event_read_epoch.simple().to_string()
    }
    pub(crate) fn worker_lease_now_ms(&self) -> io::Result<u64> {
        self.worker_lease_clock.now_ms()
    }
    pub(crate) fn worker_clock_snapshot(
        &self,
    ) -> io::Result<crate::worker_lease_clock::WorkerClockSample> {
        self.worker_lease_clock.snapshot()
    }
    #[cfg(test)]
    pub(crate) fn set_worker_lease_test_time(&self, value: u64) {
        self.worker_lease_clock.set_test_time(value);
    }
    #[cfg(test)]
    pub(crate) fn set_worker_queue_test_time(&self, value: u64) {
        self.worker_lease_clock.set_test_utc_time(value);
    }

    /// Stable durable identity for operator resume binding; paths and ports
    /// may be reused for a different authority after a restore/reinitialize.
    pub(crate) fn authority_uuid(&self) -> Uuid {
        self.entities.authority_uuid()
    }

    /// Builds the snapshot read-replica persona for the query lane
    /// (docs/STORAGE_TOFUDB.md §Snapshot read lane). Runs under the caller's
    /// authority-mutex hold and never touches the mount: a handful of Arc
    /// clones plus one registered MVCC reader pin. Query dispatch executes
    /// the identical semantic layer outside the authority mutex against the
    /// pinned snapshot; mutation entry points on the replica fail closed.
    pub fn build_read_replica(&self) -> io::Result<AuthorityDatabase> {
        Ok(AuthorityDatabase {
            entities: self.entities.build_read_replica()?,
            event_read_epoch: self.event_read_epoch,
            worker_lease_clock: Arc::clone(&self.worker_lease_clock),
            foreground_admissions: std::sync::Arc::clone(&self.foreground_admissions),
            canonical_json_cache: Arc::clone(&self.canonical_json_cache),
            decoded_chunk_cache: Arc::clone(&self.decoded_chunk_cache),
            published_query_options: None,
            published_query_results: None,
            logical_outbox: self.logical_outbox.clone(),
            preparation: None,
            timer_live_capacity: self.timer_live_capacity,
            // Replicas serve queries only; the collector belongs to the writer.
            online_gc: std::sync::Mutex::new(None),
            backup_anchors: std::sync::Mutex::new(Vec::new()),
        })
    }

    pub fn configure_logical_outbox(
        &mut self,
        encryption_key: &[u8; 32],
        _legacy_pending_bytes: u64,
        _legacy_record_bytes: usize,
    ) -> io::Result<()> {
        // Legacy size arguments remain source-compatible but never deny a
        // captured business mutation. Blob storage owns durable backlog.
        self.logical_outbox = Some(LogicalOutboxRuntime {
            cipher: LogicalOutboxCipher::new(encryption_key),
        });
        self.refresh_published_query_options();
        Ok(())
    }

    pub(crate) fn configure_timer_live_capacity(&mut self, _capacity: usize) -> io::Result<()> {
        self.timer_live_capacity = 0;
        self.refresh_published_query_options();
        Ok(())
    }



    /// Resize the hot-data caches from the probed launch budget. Both caches
    /// live in this process's own heap (unreclaimable on swapless hosts), so
    /// keeping the live working set here instead of in the kernel page cache
    /// is what survives a host memory-pressure reclaim window.
    pub(crate) fn configure_hot_data_caches(
        &self,
        block_cache_bytes: usize,
        entity_page_cache_bytes: usize,
    ) -> io::Result<()> {
        self.block_store_shared()
            .set_cache_capacity_bytes(block_cache_bytes)?;
        self.entities
            .set_page_cache_capacity_bytes(entity_page_cache_bytes)?;
        self.canonical_json_cache.configure_capacity(block_cache_bytes / crate::canonical_json_bytes::cache::HOT_CACHE_BUDGET_DIVISOR);
        self.decoded_chunk_cache.configure_capacity(block_cache_bytes / crate::blob::decode_cache::HOT_CACHE_BUDGET_DIVISOR);
        if let Some(cache) = &self.published_query_results {
            cache.configure(block_cache_bytes / crate::canonical_json_bytes::cache::HOT_CACHE_BUDGET_DIVISOR);
        }
        Ok(())
    }

    pub(crate) fn configure_blob_codecs(&self, snapshot: crate::resource_probe::LaunchResourceSnapshot) -> io::Result<()> {
        self.entities.writer_engine()?.shared_blob_encoder().configure(snapshot)
    }

    pub(crate) fn blob_codec_pool_observer(&self) -> io::Result<crate::blob::BlobCodecPoolObserver> {
        Ok(self.entities.writer_engine()?.shared_blob_encoder().observer())
    }

    /// Applies the probe-derived write-path budgets at open: the overlay
    /// merge triggers (memory headroom share + WAL geometry), the per-step
    /// checkpoint materialization budget (volume-class aware), and the
    /// verified open-segment index cache. Explicit env overrides were
    /// already folded into the supplied values by the launch probe's
    /// `DaemonResourceBudget` derivation.
    pub(crate) fn configure_write_path_budgets(
        &mut self,
        overlay_budget_bytes: usize,
        overlay_wal_watermark_bytes: u64,
        checkpoint_materialize_batch_bytes: u64,
        payload_segment_cache_bytes: usize,
    ) -> io::Result<()> {
        self.entities
            .set_overlay_trigger_budgets(overlay_budget_bytes, overlay_wal_watermark_bytes);
        self.entities
            .writer_engine_mut()?
            .set_checkpoint_materialize_batch_bytes(checkpoint_materialize_batch_bytes)?;
        self.block_store_shared()
            .set_segment_index_cache_bytes(payload_segment_cache_bytes)?;
        Ok(())
    }

    pub(crate) fn timer_live_capacity(&self) -> usize {
        self.timer_live_capacity
    }

    pub fn compact_history(
        &mut self,
        maximum_retained_segments: usize,
    ) -> io::Result<HistoryCompactionMetrics> {
        if maximum_retained_segments == 0 || maximum_retained_segments > crate::history::MAX_HISTORY_SEGMENTS {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "history retained-segment bound is invalid"));
        }
        // Explicit maintenance may materialize pending deltas; automatic
        // maintenance instead respects the replay fence without a full merge.
        self.entities.merge_overlay_fully()?;
        self.entities.writer_engine_mut()?.compact_history(maximum_retained_segments)
    }

    #[cfg(test)]
    pub(crate) fn engine_mut_for_test(&mut self) -> &mut crate::engine::Engine {
        self.entities
            .writer_engine_mut()
            .expect("test engine always has the writer persona")
    }

    pub(crate) fn try_prepare_history_compaction(&mut self, maximum: usize)
        -> io::Result<Option<crate::engine::PreparedHistoryCompaction>> {
        if maximum == 0 || maximum > crate::history::MAX_HISTORY_SEGMENTS {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "history retained-segment bound is invalid"));
        }
        let first = self.entities.overlay_replay_first_sequence()?;
        let engine = self.entities.writer_engine_mut()?;
        let retained = engine.history_retention_for_replay(maximum, first);
        let count = engine.history_segment_count();
        if count <= retained { return Ok(None); }
        // This automatic publisher shares the CONTROL lane with WAL rotation.
        // Retire only a launch-sized source slice, then release the lane so
        // rotation can progress between rounds. Explicit operator compaction
        // retains its complete requested suffix behavior.
        let slice = (engine.checkpoint_materialize_batch_bytes
            / crate::history::HISTORY_SEGMENT_MAX_BYTES as u64).max(1) as usize;
        engine.try_prepare_history_compaction(retained.max(count.saturating_sub(slice)))
    }

    pub(crate) fn finish_history_compaction(&mut self, prepared: crate::engine::PreparedHistoryCompaction,
        outcome: io::Result<crate::engine::HistoryCompactionOutcome>) -> io::Result<HistoryCompactionMetrics> {
        self.entities.writer_engine_mut()?.finish_history_compaction(prepared, outcome)
    }

    pub(crate) fn publish_completed_pack_if_capacity(&mut self) -> io::Result<bool> {
        self.entities
            .writer_engine_mut()?
            .publish_completed_pack_if_capacity()
    }

    /// Mutex-free catalog publication, phase 1 (see engine): prepares a
    /// completed background pack for publication without any fsync.
    pub(crate) fn try_prepare_pack_adoption(
        &mut self,
    ) -> io::Result<crate::engine::PackAdoptionPreparation> {
        self.entities.writer_engine_mut()?.try_prepare_pack_adoption()
    }

    /// Mutex-free catalog publication, phase 3: installs the published
    /// catalog and reconciles with commits that advanced during the window.
    pub(crate) fn finish_pack_adoption(
        &mut self,
        prepared: Box<crate::engine::PreparedPackAdoption>,
        outcome: io::Result<()>,
    ) -> io::Result<bool> {
        self.entities
            .writer_engine_mut()?
            .finish_pack_adoption(prepared, outcome)
    }

    pub(crate) fn set_wal_durability(
        &mut self,
        durability: crate::wal::WalDurability,
    ) -> io::Result<()> {
        self.entities.writer_engine_mut()?.set_wal_durability(durability)
    }

    pub(crate) fn start_wal_flusher(
        &mut self,
        interval: std::time::Duration,
    ) -> io::Result<()> {
        self.entities.writer_engine_mut()?.start_wal_flusher(interval)
    }

    /// Attaches a daemon-owned barrier coordinator: strict commits then post
    /// their durability witness and callers wait outside the authority
    /// mutex (group-barrier pipelining).
    pub(crate) fn attach_wal_barrier(&mut self, barrier: &crate::wal::WalBarrier) -> io::Result<()> {
        self.entities.writer_engine_mut()?.attach_wal_barrier(barrier)
    }

    /// Capture under the authority mutex, wait outside it before publishing a
    /// durable dependent projection. Offsets alone are unsafe across rotation.
    pub(crate) fn wal_durability_witness(
        &self,
    ) -> io::Result<Option<(crate::wal::WalBarrier, crate::wal::WalWitness)>> {
        let engine = self.entities.writer_engine()?;
        match engine.wal_barrier() {
            Some(barrier) => {
                if let Some(error) = barrier.barrier_error() {
                    return Err(error);
                }
                let witness = barrier.witness(engine.wal_len())?;
                Ok(Some((barrier, witness)))
            }
            None if engine.wal_durability() == crate::wal::WalDurability::Strict => Ok(None),
            None => Err(io::Error::other(
                "deferred WAL requires a durability barrier for projection",
            )),
        }
    }

    /// The active-log length witness for `WalBarrier::wait_synced`.
    pub(crate) fn wal_len(&self) -> io::Result<u64> {
        Ok(self.entities.writer_engine()?.wal_len())
    }

    /// Reject a new write before semantic evaluation allocates/stages payloads.
    /// Receipt replays and read operations remain available during rotation.
    pub(crate) fn require_write_admission(&self) -> io::Result<()> {
        if self.preparation.is_some() {
            return self.entities.require_usable_authority();
        }
        self.entities.writer_engine()?.require_write_admission()
    }

    /// Whether an append risks rotation backpressure right now: a rotation
    /// owns the lane, or the active log is close enough to the hard ceiling
    /// that a typical append could cross it. The server layer parks command
    /// dispatches while this holds instead of rejecting them; the margin
    /// errs toward parking, and the in-mutex backpressure stays as the
    /// fail-safe for appends larger than the margin.
    pub(crate) fn rotation_blocks_appends(&self) -> bool {
        // Cannot inspect the lane: leave rejection to the in-mutex
        // backpressure fail-safe rather than stalling writers.
        let Ok(engine) = self.entities.writer_engine() else {
            return false;
        };
        engine.rotation_in_flight()
            || engine.online_rotation_blocks_writes()
            || engine.wal_len() >= crate::ACTIVE_LOG_MAX_BYTES - ROTATION_PARK_WAL_MARGIN_BYTES
    }

    /// Attaches the daemon rotation driver: the soft-rotation path then
    /// prepares async rotations instead of synchronous checkpoints.
    pub(crate) fn attach_rotation_driver(&mut self) -> io::Result<()> {
        self.entities.writer_engine_mut()?.attach_rotation_driver();
        Ok(())
    }

    /// Registers the rotation wakeup barrier: every prepared rotation
    /// signals it, so the driver starts immediately instead of polling.
    pub(crate) fn attach_rotation_wakeup(
        &mut self,
        wakeup: std::sync::Arc<crate::rotation_barrier::RotationBarrier>,
    ) -> io::Result<()> {
        self.entities
            .writer_engine_mut()?
            .attach_rotation_wakeup(wakeup);
        Ok(())
    }

    /// Hands a prepared rotation (if any) to the rotation driver.
    pub(crate) fn take_prepared_rotation(
        &mut self,
    ) -> io::Result<Option<crate::engine::RotationWork>> {
        self.entities.writer_engine_mut()?.take_prepared_rotation()
    }

    /// Installs the checkpoint tail after the unlocked segment encoding.
    pub(crate) fn finish_rotation_checkpoint(
        &mut self,
        snapshot: Box<crate::engine::PreparedRotationCheckpoint>,
        outcome: io::Result<crate::engine::RotationSegmentOutcome>,
    ) -> io::Result<u64> {
        self.entities
            .writer_engine_mut()?
            .finish_rotation_checkpoint(snapshot, outcome)
    }

    pub(crate) fn prepare_rotation_publication(
        &mut self,
        snapshot: Box<crate::engine::PreparedRotationCheckpoint>,
        outcome: io::Result<crate::engine::RotationSegmentOutcome>,
    ) -> io::Result<crate::engine::RotationPublication> {
        self.entities.writer_engine_mut()?.prepare_rotation_publication(snapshot, outcome)
    }

    pub(crate) fn finish_rotation_publication(
        &mut self,
        prepared: Box<crate::engine::PreparedRotationPublication>,
        outcome: io::Result<()>,
    ) -> io::Result<u64> {
        self.entities.writer_engine_mut()?.finish_rotation_publication(prepared, outcome)
    }

    /// The WAL worker must inherit the authority's actual VFS capabilities.
    pub(crate) fn wal_vfs(&self) -> io::Result<Arc<dyn crate::vfs::Vfs>> {
        Ok(self.entities.writer_engine()?.vfs())
    }

    pub(crate) fn wal_path(&self) -> io::Result<std::path::PathBuf> {
        self.entities.writer_engine()?.wal_path()
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
        let mut session = self.begin_garbage_collection(budget)?;
        let result = match mode {
            AuthorityGarbageCollectionMode::Plan => session.plan(),
            AuthorityGarbageCollectionMode::Execute => session.collect(),
        };
        let cleanup = session.finish();
        result.and_then(|metrics| cleanup.map(|_| metrics))
    }

    /// Holds the exclusive engine borrow until checked finish or drop. No
    /// business transaction, checkpoint or history retirement can run inside
    /// this scope, so complete data marks are reusable across physical rounds.
    pub fn begin_garbage_collection(
        &mut self,
        budget: AuthorityGarbageCollectionBudget,
    ) -> io::Result<AuthorityGarbageCollectionSession<'_>> {
        if self.snapshot_pin_metrics()?.active_handles != 0 {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "authority GC defers while MVCC snapshots are active",
            ));
        }
        // Reachability walks the tree: any committed-but-unmerged overlay
        // deltas must land before planning, or live content would be
        // collected as garbage.
        self.entities.merge_overlay_fully()?;
        AuthorityGarbageCollectionSession::new(self.entities.writer_engine_mut()?, budget)
    }

    /// Installs the daemon's online collector doorbell (one per process).
    pub fn set_online_garbage_collection(
        &self,
        handle: crate::authority_gc_online::OnlineGarbageCollectionHandle,
    ) {
        *self
            .online_gc
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(handle);
    }

    /// The daemon's online collector doorbell, when it runs. `system.reclaim`
    /// rings it; control operations read its metrics.
    pub fn online_garbage_collection(
        &self,
    ) -> Option<crate::authority_gc_online::OnlineGarbageCollectionHandle> {
        self.online_gc
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }

    /// Anchor-phase output for one online-backup epoch: the checkpoint
    /// manifest plus the registered anchor token and the shared source
    /// store. The off-mutex copy phase needs nothing else.
    /// Startup validation for the online-backup driver's directory.
    pub(crate) fn validate_online_backup_directory(&self, backup_dir: &Path) -> io::Result<()> {
        crate::backup::require_disjoint_paths(
            self.entities.writer_engine()?.data_dir(),
            backup_dir,
            "backup directory must not contain or be contained by the authority",
        )
    }

    /// Captures the checkpoint anchor under the caller's short hold:
    /// overlay merge, checkpoint when commits are pending, manifest, and
    /// anchor registration, all in one authority-mutex hold.
    pub(crate) fn capture_backup_anchor(&mut self) -> io::Result<BackupAnchorCapture> {
        self.entities.merge_overlay_fully()?;
        let (manifest, projection, source) = {
            let engine = self.entities.writer_engine_mut()?;
            engine.require_usable_authority()?;
            if engine.has_active_transactions() {
                engine.checkpoint()?;
            }
            let manifest = crate::backup::BackupManifest::from_control(engine.state())?;
            let snapshot = engine.capture_online_gc_mark_snapshot(Vec::new())?;
            let projection = crate::online_backup::BackupAnchorProjection {
                root: snapshot.authority_state_root,
                history_manifest_block_id: snapshot.history_manifest_block_id,
                history_segments: snapshot.history_segments.clone(),
            };
            (manifest, projection, engine.block_store_shared())
        };
        let anchor_token = self.register_backup_anchor(projection);
        Ok(BackupAnchorCapture {
            manifest,
            anchor_token,
            source,
        })
    }

    /// Best-effort wipe of the online collector's spill runs; used by the
    /// driver on graceful stop so no GC scratch survives a clean shutdown.
    pub(crate) fn wipe_online_gc_work_directories(&self) -> io::Result<()> {
        let engine = self.entities.writer_engine()?;
        crate::authority_gc_online::wipe_work_directories(
            engine.data_dir(),
            engine.vfs().as_ref(),
        );
        Ok(())
    }

    /// Registers one in-progress backup's anchor projection. Registered
    /// anchors join every online-GC mark, so the collector can never
    /// reclaim anything the off-mutex copy is still reading (rule S2
    /// extended to backup anchors). Returns a monotonic deregistration
    /// token.
    pub(crate) fn register_backup_anchor(
        &self,
        anchor: crate::online_backup::BackupAnchorProjection,
    ) -> u64 {
        let mut anchors = self
            .backup_anchors
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let token = anchors.last().map_or(0, |(token, _)| token + 1);
        anchors.push((token, anchor));
        token
    }

    pub(crate) fn deregister_backup_anchor(&self, token: u64) {
        self.backup_anchors
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .retain(|(candidate, _)| *candidate != token);
    }

    /// Offline operator access to the writer persona (verify/salvage and
    /// the offline CLI share it): the merge-before-anchor contract lives
    /// at the call site.
    pub(crate) fn writer_engine(&self) -> io::Result<&Engine> {
        self.entities.writer_engine()
    }

    pub(crate) fn merge_overlay_fully_for_operators(&mut self) -> io::Result<()> {
        self.entities.merge_overlay_fully()
    }

    /// Captures one online-GC epoch's mark inputs under the caller's
    /// authority-mutex hold and re-arms the relink fence in the same hold
    /// (docs/TOFUDB_ONLINE_GC.md rules S2/S7). Never performs file I/O.
    pub(crate) fn online_gc_capture(&self, maximum_snapshot_bytes: u64) -> io::Result<OnlineGcCapture> {
        let engine = self.entities.writer_engine()?;
        let mut pinned_roots: Vec<crate::block::BlockId> = self
            .entities
            .pinned_snapshots()?
            .into_iter()
            .filter_map(|snapshot| snapshot.root)
            .collect();
        let anchors = self
            .backup_anchors
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone();
        pinned_roots.extend(anchors.iter().filter_map(|(_, anchor)| anchor.root));
        let mut snapshot = engine.capture_online_gc_mark_snapshot_off_authority(pinned_roots, maximum_snapshot_bytes)?;
        for (_, anchor) in anchors.iter() {
            snapshot
                .history_segments
                .extend(anchor.history_segments.iter().cloned());
            if let Some(manifest_id) = anchor.history_manifest_block_id {
                snapshot.committed_block_ids.push(manifest_id);
            }
        }
        let snapshot = snapshot;
        let blocks = engine.block_store_shared();
        blocks.begin_relink_observation();
        Ok(OnlineGcCapture {
            snapshot,
            blocks,
            vfs: engine.vfs(),
            data_dir: engine.data_dir().to_path_buf(),
        })
    }

    /// Deletes one bounded batch of confirmed loose-block victims under the
    /// caller's authority hold, filtering anything re-referenced since the
    /// epoch's mark snapshot (relink fence, rule S7). `WouldBlock` when the
    /// fence overflowed or the physical lane is busy: retry next epoch.
    pub(crate) fn online_gc_delete_loose(
        &mut self,
        confirmed: &[BlockId],
    ) -> io::Result<crate::block::BlockRemovalMetrics> {
        let engine = self.entities.writer_engine_mut()?;
        engine.require_usable_authority()?;
        engine.require_physical_maintenance_available()?;
        let fence = engine.block_store().relink_fence_snapshot();
        if fence.overflow {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "relink fence overflow; defer the batch to the next epoch",
            ));
        }
        let victims = confirmed
            .iter()
            .filter(|block_id| !fence.ids.contains(*block_id))
            .copied()
            .collect::<Vec<_>>();
        if victims.is_empty() {
            return Ok(crate::block::BlockRemovalMetrics::default());
        }
        engine.block_store().remove_blocks(&victims)
    }

    /// Plans one orphan payload-segment file under the caller's hold; the
    /// plan consults the background-pack unpublished set.
    pub(crate) fn online_gc_plan_orphan(
        &self,
    ) -> io::Result<crate::payload_segment::PayloadSegmentOrphanPlan> {
        self.entities.writer_engine()?.plan_payload_segment_orphan()
    }

    /// Deletes one two-epoch-confirmed orphan segment file under the
    /// caller's hold; re-validates catalog/background membership internally.
    pub(crate) fn online_gc_remove_orphan(
        &self,
        candidate: &crate::payload_segment::PayloadSegmentOrphanCandidate,
    ) -> io::Result<u64> {
        self.entities
            .writer_engine()?
            .remove_payload_segment_orphan(candidate)
    }

    /// Publishes one off-mutex-built segment rewrite under the caller's hold.
    pub(crate) fn online_gc_finish_segment_reclaim(
        &mut self,
        segment_id: Uuid,
        expected: &crate::payload_manifest::PayloadSegmentReference,
        retained_ids: &[BlockId],
        replacement: Option<crate::payload_manifest::PayloadSegmentReference>,
    ) -> io::Result<crate::engine::PayloadSegmentGcMetrics> {
        self.entities.writer_engine_mut()?.finish_segment_reclaim(
            segment_id,
            expected,
            retained_ids,
            replacement,
        )
    }

    /// Publishes one off-mutex-built segment consolidation under the
    /// caller's hold (catalog-slot recovery at pressure).
    pub(crate) fn online_gc_finish_consolidation(
        &mut self,
        victims: &[crate::payload_manifest::PayloadSegmentReference],
        retained_per_victim: &[Vec<BlockId>],
        replacement: Option<crate::payload_manifest::PayloadSegmentReference>,
    ) -> io::Result<crate::engine::SegmentConsolidationMetrics> {
        self.entities
            .writer_engine_mut()?
            .finish_segment_consolidation(victims, retained_per_victim, replacement)
    }

    /// Publishes one off-mutex-built loose-block compaction under the
    /// caller's hold.
    pub(crate) fn online_gc_finish_compaction(
        &mut self,
        sorted_ids: &[BlockId],
        built_reference: crate::payload_manifest::PayloadSegmentReference,
    ) -> io::Result<crate::engine::PayloadCompactionMetrics> {
        self.entities
            .writer_engine_mut()?
            .finish_payload_compaction(sorted_ids, built_reference)
    }

    #[cfg(test)]
    pub(crate) fn authority_state_root(&self) -> Option<BlockId> {
        self.entities
            .writer_engine()
            .expect("test authority always has the writer persona")
            .state()
            .authority_state_root
            .flatten()
    }

    #[cfg(test)]
    pub(crate) fn checkpoint_for_test(&mut self) -> io::Result<u64> {
        // Same contract as the daemon shutdown tail: a checkpoint never
        // publishes with pending overlay deltas (they would strand below the
        // checkpoint boundary with no active window to replay them).
        self.entities.merge_overlay_fully()?;
        self.entities.writer_engine_mut()?.checkpoint()
    }

    #[cfg(test)]
    pub(crate) fn history_segment_count_for_test(&self) -> usize {
        self.entities
            .writer_engine()
            .expect("test authority always has the writer persona")
            .history_segment_count()
    }

    #[cfg(test)]
    pub(crate) fn write_orphan_block_for_test(&self, payload: &[u8]) -> io::Result<BlockId> {
        // Orphan fixtures need a real on-disk loose block that GC planning
        // can discover by scanning shards; the deferred write path stages
        // payloads in memory only, so it cannot model an orphan.
        self.entities.writer_engine()?.block_store().put_loose(payload)
    }

    #[cfg(test)]
    pub(crate) fn read_block_for_test(&self, block_id: BlockId) -> io::Result<Vec<u8>> {
        self.entities.writer_engine()?.read_block(block_id)
    }

    /// Starts this authority's bounded payload-packing and WAL-retirement worker.
    pub fn start_background_packer(&self) -> io::Result<crate::background_pack::BackgroundPacker> {
        self.entities.writer_engine()?.start_background_packer()
    }

    pub fn start_background_packer_paced(
        &self,
        pace_bytes_per_second: u64,
    ) -> io::Result<crate::background_pack::BackgroundPacker> {
        self.entities
            .writer_engine()?
            .start_background_packer_paced(pace_bytes_per_second)
    }

    pub fn start_background_packer_adaptive(
        &self,
        adaptive_start_rate: Option<u64>,
    ) -> io::Result<crate::background_pack::BackgroundPacker> {
        self.entities
            .writer_engine()?
            .start_background_packer_adaptive(adaptive_start_rate)
    }

    pub fn block_store_shared(&self) -> Arc<crate::block::BlockStore> {
        self.entities.block_store_shared()
    }

    #[cfg(test)]
    pub(crate) fn commit_block_payload_for_test(&mut self, payload: &[u8]) -> io::Result<BlockId> {
        Ok(self
            .entities
            .writer_engine_mut()?
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
            .writer_engine_mut()?
            .commit_transaction(b"gc-payload-references", &payload_slices)?
            .block_ids)
    }

    #[cfg(test)]
    pub(crate) fn compact_payload_blocks_for_test(
        &mut self,
        block_ids: &[BlockId],
    ) -> io::Result<()> {
        self.entities
            .writer_engine_mut()?
            .compact_payload_blocks(block_ids)
            .map(|_| ())
    }

    #[cfg(test)]
    pub(crate) fn payload_segment_count_for_test(&self) -> usize {
        self.entities
            .writer_engine()
            .expect("test authority always has the writer persona")
            .payload_segment_count()
    }

    #[cfg(test)]
    pub(crate) fn create_orphan_payload_segment_for_test(
        &self,
        block_id: BlockId,
    ) -> io::Result<Uuid> {
        self.entities
            .writer_engine()?
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
            self.entities.writer_engine()?.state().durable_sequence
        };
        Ok(ConversationActivityBackfillProgress {
            processed_rows: batch.processed_rows,
            source_bytes: batch.source_bytes,
            complete: batch.complete,
            committed,
            durable_sequence,
        })
    }

    /// Operator-only tenant-wide checkout claim upgrade; not a wire command.
    /// The caller holds the exclusive authority lease, as for activity backfill.
    pub fn backfill_integration_checkout_claims(
        &mut self,
        tenant_id: u64,
        owner_user_id: u64,
        maximum_rows: usize,
    ) -> io::Result<IntegrationCheckoutBackfillProgress> {
        if tenant_id == 0 || owner_user_id == 0 {
            return Err(invalid_input("checkout backfill scope IDs must be positive"));
        }
        let mut transaction = self.begin_with_identity_claim_scopes(tenant_id, owner_user_id)?;
        let batch = crate::integration_workspace::backfill_checkout_claims(
            self, &mut transaction, maximum_rows,
        )?;
        let durable_sequence = if batch.changed {
            self.commit(transaction)?.transaction_sequence
        } else {
            self.entities.writer_engine()?.state().durable_sequence
        };
        Ok(IntegrationCheckoutBackfillProgress {
            processed_rows: batch.processed_rows,
            after_id: batch.after_id,
            complete: batch.complete,
            committed: batch.changed,
            durable_sequence,
        })
    }

    pub(crate) fn begin_billing_migration(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
    ) -> io::Result<AuthorityTransaction> {
        let prefixes = [
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
        self.begin_with_entity_scopes(tenant_id, owner_user_id, prefixes)
    }

    pub(crate) fn begin_with_identity_claim_scopes(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
    ) -> io::Result<AuthorityTransaction> {
        self.begin_semantic_scopes(tenant_id, owner_user_id, false)
    }

    pub(crate) fn begin_with_paper_document_scope(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
    ) -> io::Result<AuthorityTransaction> {
        self.begin_semantic_scopes(tenant_id, owner_user_id, true)
    }

    fn begin_semantic_scopes(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        include_paper_document_budget: bool,
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
            crate::generated_tofudb_ir::ORCHESTRATION_RUN_PENDING_CONTINUITY_INDEX_NAMESPACE,
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
            crate::generated_paper_build::BUDGET_NAMESPACE,
            KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE,
        ]
        .into_iter()
        .chain(
            include_paper_document_budget
                .then_some(crate::generated_paper_build::DOCUMENT_BUDGET_NAMESPACE),
        )
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
            prepared_blobs: self.preparation.clone(),
            conversation_notices: std::collections::BTreeMap::new(),
            authority_uuid: self.entities.authority_uuid(),
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
            deferred_outbox_capture: None,
            deferred_indexes: Vec::new(),
            has_business_mutation: false,
            content_free_diagnostic_outbox_exemption: false,
            internal_maintenance: false,
        })
    }

    /// Resident immutable overlay bytes include reader-held blocks; pending
    /// bytes count only unmaterialized work and drive maintenance admission.
    pub fn overlay_memory_bytes(&self) -> io::Result<(usize, usize)> {
        self.entities.overlay_memory_bytes()
    }

    pub(crate) fn query_snapshot(&self) -> crate::entity::EntitySnapshot {
        self.entities.current_snapshot()
    }

    pub fn snapshot_pin_metrics(&self) -> io::Result<EntitySnapshotPinMetrics> {
        self.entities.snapshot_pin_metrics()
    }

    pub fn stage_persistent_snapshot_pin(
        &mut self,
        transaction: &mut AuthorityTransaction,
        pin_id: &[u8],
    ) -> io::Result<EntitySnapshot> {
        self.entities
            .stage_persistent_snapshot_pin(&mut transaction.entity, pin_id)
    }

    pub fn stage_persistent_range_snapshot_pin(
        &mut self,
        transaction: &mut AuthorityTransaction,
        pin_id: &[u8],
        ranges: &[(EntityKey, EntityKey)],
    ) -> io::Result<EntitySnapshot> {
        self.entities
            .stage_persistent_range_snapshot_pin(&mut transaction.entity, pin_id, ranges)
    }

    pub fn stage_persistent_range_snapshot_restore(
        &mut self,
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
            prepared_blobs: None,
            conversation_notices: std::collections::BTreeMap::new(),
            authority_uuid: self.entities.authority_uuid(),
            tenant_id,
            owner_user_id,
            entity,
            stream_appends: Vec::new(),
            stream_bytes: 0,
            staged_blob_block_ids: BTreeSet::new(),
            receipt_record: None,
            logical_outbox_record: None,
            deferred_outbox_capture: None,
            deferred_indexes: Vec::new(),
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
        let append_bytes = events.iter().try_fold(0_usize, |total, event| {
            total
                .checked_add(event.encoded_len())
                .ok_or_else(|| invalid_input("authority stream byte count overflow"))
        })?;
        let next_stream_bytes = transaction
            .stream_bytes
            .checked_add(append_bytes)
            .ok_or_else(|| invalid_input("authority stream byte count overflow"))?;
        if let Some(index) = existing_append {
            let pending = &mut transaction.stream_appends[index];
            let pending_next = pending
                .expected_next_sequence
                .checked_add(pending.events.len() as u64)
                .ok_or_else(|| invalid_data("pending stream sequence overflow"))?;
            if expected_next_sequence != pending_next {
                return Err(conflict("pending stream expected-position witness changed"));
            }
            pending.events.len().checked_add(events.len())
                .and_then(|count| u32::try_from(count).ok())
                .ok_or_else(|| invalid_input("pending stream event count exceeds its u32 representation"))?;
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
        let staged = crate::blob::stage_blob_with_backend(
            self.blob_staging_backend()?,
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
        let staged = crate::blob::stage_blob_with_backend(
            self.blob_staging_backend()?,
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
        if transaction.staged_blob_block_ids.len().checked_add(additional_block_ids)
            .and_then(|count| u32::try_from(count).ok()).is_none()
        {
            return Err(invalid_input(
                "authority staged blob count exceeds its u32 representation",
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
            self.entities.backend(),
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
            self.entities.backend(),
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
            self.entities.backend(),
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
            self.entities.backend(),
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

    pub fn decoded_chunk_cache_observer(&self) -> DecodedChunkCacheObserver {
        self.decoded_chunk_cache.observer()
    }

    pub fn canonical_json_cache_observer(&self) -> CanonicalProofCacheObserver {
        self.canonical_json_cache.observer()
    }

    pub(crate) fn canonicalize_verified_blob<'a>(
        &self, blob: &'a crate::blob::VerifiedBlobBytes,
    ) -> Result<crate::canonical_json_bytes::CanonicalJsonBody<'a>, serde_json::Error> {
        self.canonical_json_cache.canonicalize_blob(blob)
    }

    pub fn blob_reader(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<BlobReader<'_>> {
        BlobReader::open(self.blob_read_backend(), self.entities.commit_witness()?, tenant_id, owner_user_id, reference).map(|reader| reader.with_decode_cache(&self.decoded_chunk_cache))
    }

    pub(crate) fn reachable_blob_reader(
        &self,
        tenant_id: u64,
        owner_user_id: u64,
        reference: BlobReference,
    ) -> io::Result<BlobReader<'_>> {
        BlobReader::open_reachable(self.blob_read_backend(), tenant_id, owner_user_id, reference).map(|reader| reader.with_decode_cache(&self.decoded_chunk_cache))
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
        if transaction.logical_outbox_record.is_some() || transaction.deferred_outbox_capture.is_some() {
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
        let record_bytes = encoded_record.len() as u64;
        let next_pending_bytes = pending_bytes
            .checked_add(record_bytes)
            .ok_or_else(|| invalid_data("logical outbox pending byte count overflow"))?;
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
            || transaction.deferred_outbox_capture.is_some()
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
        if limit == 0 {
            return Err(invalid_input("invalid logical outbox fetch limit"));
        }
        if maximum_bytes == 0 {
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
            // Byte hints choose page boundaries, never strand its first record.
            if next_fetched_bytes > maximum_bytes && !pending.is_empty() {
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
        self.entities.is_restart_required()
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
                || !crate::search_dirty::valid_token(&token.value)
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
        self.receipt_lookup_any(transaction, command_id, operation, request_digest, None)
    }

    /// Replay lookup that additionally accepts one historical request digest.
    ///
    /// The legacy v1 authority digested the request payload exactly as
    /// received; the storage.v2 client injects the session owner into every
    /// payload, so a migrated receipt written for a caller that omitted
    /// `user_id` no longer matches the primary digest. `alternate_digest`
    /// carries that ownerless spelling. Acceptance stays fenced: the receipt
    /// still lives under the exact (tenant, owner, command_id) key and must
    /// name the same operation, so the widened match cannot alias a genuinely
    /// different request. New receipts are always stored with the primary
    /// digest only.
    pub fn receipt_lookup_any(
        &self,
        transaction: &mut AuthorityTransaction,
        command_id: &str,
        operation: &str,
        request_digest: [u8; 32],
        alternate_digest: Option<[u8; 32]>,
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
        let digest_matches = receipt.request_digest == request_digest
            || alternate_digest.is_some_and(|alternate| receipt.request_digest == alternate);
        if receipt.operation != operation || !digest_matches {
            return Err(io::Error::new(
                io::ErrorKind::WouldBlock,
                "command ID was reused for a different request",
            ));
        }
        let encoded_response = self.receipt_response_bytes(transaction, receipt.response)?;
        Ok(Some(decode_receipt_response(&encoded_response)?))
    }

    fn receipt_response_bytes(
        &self,
        transaction: &AuthorityTransaction,
        response: StoredReceiptResponse,
    ) -> io::Result<Vec<u8>> {
        Ok(match response {
            StoredReceiptResponse::Inline(response) => response,
            StoredReceiptResponse::Blob(reference) => {
                let mut reader = self.reachable_blob_reader(
                    transaction.tenant_id,
                    transaction.owner_user_id,
                    reference,
                )?;
                let mut response = Vec::new();
                while let Some(chunk) = reader.next_chunk()? {
                    response.try_reserve(chunk.len()).map_err(|_| io::Error::new(io::ErrorKind::OutOfMemory,"receipt materialization allocation failed"))?;
                    response.extend_from_slice(&chunk);
                    if response.len() as u64 > reference.logical_bytes {
                        return Err(io::Error::new(
                            io::ErrorKind::InvalidData,
                            "receipt blob exceeds its declared byte length",
                        ));
                    }
                }
                response
            }
        })
    }

    pub(crate) fn receipt_snapshot(
        &self,
        transaction: &mut AuthorityTransaction,
        command_key: &[u8; 32],
    ) -> io::Result<Option<ReceiptSnapshot>> {
        let key = EntityKey::new(
            transaction.tenant_id,
            transaction.owner_user_id,
            "command_receipt",
            command_key,
        )?;
        let Some(encoded) = self.entity_get(transaction, &key)? else {
            return Ok(None);
        };
        let receipt = CommandReceipt::decode(&encoded)?;
        Ok(Some(ReceiptSnapshot {
            encoded_response: self.receipt_response_bytes(transaction, receipt.response)?,
            operation: receipt.operation,
            request_digest: receipt.request_digest,
            committed_at_ms: receipt.committed_at_ms,
        }))
    }

    pub(crate) fn receipt_restore_snapshot(
        &self,
        transaction: &mut AuthorityTransaction,
        command_key: [u8; 32],
        snapshot: ReceiptSnapshot,
    ) -> io::Result<bool> {
        validate_receipt_identity(&snapshot.operation, snapshot.committed_at_ms)?;
        // Admission is bounded by the existing codec, before staging any blob.
        let raw = decode_receipt_response(&snapshot.encoded_response)?;
        serde_json::from_slice::<serde_json::Value>(&raw)
            .map_err(|_| invalid_input("receipt snapshot response is not JSON"))?;
        if let Some(existing) = self.receipt_snapshot(transaction, &command_key)? {
            if existing != snapshot {
                return Err(io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "command receipt snapshot differs",
                ));
            }
            return Ok(false);
        }
        self.receipt_insert_snapshot(transaction, command_key, snapshot)?;
        Ok(true)
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
        validate_receipt_identity(operation, committed_at_ms)?;
        self.receipt_insert_snapshot(
            transaction,
            command_receipt_key(command_id)?,
            ReceiptSnapshot {
                operation: operation.to_owned(),
                request_digest,
                committed_at_ms,
                encoded_response: encode_receipt_response(raw_response)?,
            },
        )
    }

    fn receipt_insert_snapshot(
        &self,
        transaction: &mut AuthorityTransaction,
        command_key: [u8; 32],
        snapshot: ReceiptSnapshot,
    ) -> io::Result<()> {
        if transaction.receipt_record.is_some() {
            return Err(invalid_input(
                "authority transaction contains more than one command receipt",
            ));
        }
        let entity_key = EntityKey::new(
            transaction.tenant_id,
            transaction.owner_user_id,
            "command_receipt",
            &command_key,
        )?;
        if self.entity_get(transaction, &entity_key)?.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "command receipt already exists",
            ));
        }
        let encoded_response = snapshot.encoded_response;
        let response = if encoded_response.len() <= MAX_INLINE_RECEIPT_BYTES {
            StoredReceiptResponse::Inline(encoded_response)
        } else {
            let maximum_bytes = encoded_response.len() as u64;
            let reference =
                self.stage_blob(transaction, &mut encoded_response.as_slice(), maximum_bytes)?;
            StoredReceiptResponse::Blob(reference)
        };
        let entry = CommandReceipt {
            operation: snapshot.operation,
            request_digest: snapshot.request_digest,
            committed_at_ms: snapshot.committed_at_ms,
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

    /// Write-mode switch for the deferred-application entity write overlay.
    /// Reads always merge the store whenever it is non-empty, so disabling
    /// writes on a database with replayed deltas stays exact.
    pub fn set_write_overlay_enabled(&mut self, enabled: bool) {
        self.entities.set_write_overlay_enabled(enabled);
    }

    /// Test switch for the deferred-application entity write overlay.
    #[cfg(test)]
    pub(crate) fn set_write_overlay_for_test(&mut self, enabled: bool) {
        self.entities.set_write_overlay_enabled(enabled);
    }

    /// Test switch for the overlay merge byte budget trigger.
    #[cfg(test)]
    pub(crate) fn set_overlay_budget_for_test(&mut self, bytes: usize) {
        self.entities.set_overlay_budget_for_test(bytes);
    }

    /// Merge every committed-but-unmerged overlay delta into the entity tree
    /// Commit one generic engine payload record (no authority-state
    /// statement, `authority_state_update == None`) below the semantic
    /// layer: the interleaving the 2026-09-20 replay-stranding regression
    /// test pins against pending overlay deltas.
    #[cfg(test)]
    pub(crate) fn commit_engine_payload_for_test(&mut self, payload: &[u8]) -> io::Result<()> {
        self.entities
            .writer_engine_mut()?
            .commit_transaction(payload, &[&vec![0x51; 1024]])?;
        Ok(())
    }

    /// as one batch commit (see `EntityDatabase::merge_overlay`).
    #[cfg(test)]
    pub(crate) fn merge_entity_overlay_for_test(&mut self) -> io::Result<bool> {
        let before = self.entity_overlay_entries_for_test();
        self.entities.merge_overlay_fully()?;
        Ok(before > 0)
    }

    /// Merge ONE bounded overlay chunk (a background drain slice; no-op
    /// when empty or disabled). The maintenance worker interleaves these
    /// with foreground commits so the commit-path trigger stays a backstop.
    pub fn merge_entity_overlay(&mut self) -> io::Result<bool> {
        self.entities.merge_overlay()
    }

    pub(crate) fn prepare_entity_overlay_merge(&self) -> io::Result<Option<crate::entity::OverlayMergePreparation>> {
        self.entities.prepare_overlay_merge()
    }

    pub(crate) fn finish_entity_overlay_merge(&mut self, prepared: crate::entity::PreparedOverlayMerge) -> io::Result<bool> {
        self.entities.finish_overlay_merge(prepared)
    }

    /// Whether tombstone reclamation is enabled for this authority
    /// (global flag or per-database override); the maintenance round
    /// short-circuits compaction when off.
    pub(crate) fn entity_tombstone_reclaim_is_enabled(&self) -> bool {
        self.entities.tombstone_reclaim_is_enabled()
    }

    /// Maintenance boundary for tombstone compaction (L2,
    /// docs/TOFUDB_TOMBSTONE_COMPACTION.md): pop the densest advisory hint
    /// reported by scans.
    pub(crate) fn take_entity_tombstone_compaction_hint(
        &self,
    ) -> io::Result<Option<(Vec<u8>, Vec<u8>, u32)>> {
        self.entities.take_tombstone_compaction_hint()
    }

    /// Requeue one compaction window (another scope's hint or a partially
    /// swept window) into the advisory registry.
    #[cfg(test)]
    pub(crate) fn set_entity_tombstone_reclaim_override_for_test(
        &mut self,
        enabled: Option<bool>,
    ) {
        self.entities
            .set_tombstone_reclaim_override_for_test(enabled);
    }

    pub(crate) fn record_entity_tombstone_compaction_hint(
        &self,
        start: &[u8],
        end: &[u8],
        dead_leaves: u32,
    ) -> io::Result<()> {
        self.entities
            .record_entity_tombstone_compaction_hint(start, end, dead_leaves)
    }

    /// One bounded tombstone-compaction slice (advisory, crash-atomic);
    /// see `EntityDatabase::reclaim_tombstones_in_range`.
    pub(crate) fn reclaim_entity_tombstones_in_range(
        &mut self,
        start: &[u8],
        end: &[u8],
        max_leaves: usize,
    ) -> io::Result<crate::entity::EntityCompactionProgress> {
        self.entities
            .reclaim_tombstones_in_range(start, end, max_leaves)
    }

    /// Lock-free background-drain probe for the maintenance worker: soft
    /// thresholds derived from the configured hard commit-path bounds.
    pub(crate) fn entity_overlay_soft_trigger(
        &self,
    ) -> crate::entity_overlay::EntityOverlaySoftTrigger {
        crate::entity_overlay::EntityOverlaySoftTrigger::new(
            std::sync::Arc::clone(&self.entities.overlay),
            self.entities.overlay_budget_bytes,
        )
    }

    /// Pending overlay delta entries (test assertions).
    #[cfg(test)]
    pub(crate) fn entity_overlay_entries_for_test(&self) -> usize {
        self.entities
            .overlay
            .read()
            .map(|overlay| overlay.entries())
            .unwrap_or(0)
    }

    /// Drain the deferred-application overlay (maintenance hook; no-op when
    /// empty or disabled).
    pub fn merge_entity_overlay_fully(&mut self) -> io::Result<()> {
        self.entities.merge_overlay_fully()
    }

    #[cfg(test)]
    pub(crate) fn history_blocks_for_test(&self) -> Vec<crate::block::BlockId> {
        self.entities.writer_engine().unwrap().history_segment_block_ids().to_vec()
    }

    /// Retire the active WAL window through the engine's own checkpoint
    /// (graceful-stop hook; see the daemon shutdown tail). The caller merges
    /// the overlay first — a checkpoint must never publish with pending
    /// deltas. An empty committed window is reported as `InvalidInput` by
    /// the engine and means "nothing to retire", not a failure.
    pub(crate) fn checkpoint_wal_window(&mut self) -> io::Result<u64> {
        self.entities.writer_engine_mut()?.checkpoint()
    }

    pub fn commit(
        &mut self,
        mut transaction: AuthorityTransaction,
    ) -> io::Result<AuthorityCommitResult> {
        if !EntityDatabase::transaction_is_writable(&transaction.entity) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "persistent entity snapshots are read-only",
            ));
        }
        if transaction.authority_uuid != self.entities.authority_uuid() {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied,
                "authority transaction belongs to a different database"));
        }
        let mut private_blobs_published = false;
        if transaction.deferred_outbox_capture.is_some() || !transaction.deferred_indexes.is_empty() {
            {
                let _timer = crate::fsync_stats::StageTimer::diagnostic(
                    crate::fsync_stats::STAGE_AUTHORITY_COMMIT_VALIDATE_REBASE);
                self.require_write_admission()?;
                // Validate business predicates before commit-owned index/counter
                // maintenance. Independent business writes must not conflict on
                // the physical representation of a shared secondary index.
                self.entities.rebase_validated_for_commit(&mut transaction.entity)?;
            }
            if !transaction.deferred_indexes.is_empty() {
                // Backfill may read a business document staged as a private
                // blob. Publication is safe now: validation succeeded and the
                // exclusive sequencer excludes GC deletion through commit.
                if let Some(blobs) = &transaction.prepared_blobs {
                    let _timer = crate::fsync_stats::StageTimer::diagnostic(
                        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_PRIVATE_BLOB_PUBLISH);
                    blobs.publish(self.entities.writer_engine()?)?;
                    private_blobs_published = true;
                }
                {
                    let _timer = crate::fsync_stats::StageTimer::diagnostic(
                        crate::fsync_stats::STAGE_AUTHORITY_COMMIT_INDEXES);
                    self.flush_deferred_indexes(&mut transaction)?;
                }
            }
            if let Some(capture) = transaction.deferred_outbox_capture.take() {
                let _timer = crate::fsync_stats::StageTimer::diagnostic(
                    crate::fsync_stats::STAGE_AUTHORITY_COMMIT_OUTBOX_CAPTURE);
                self.logical_outbox_capture(&mut transaction, capture)?;
            }
        }
        let AuthorityTransaction {
            prepared_blobs,
            conversation_notices,
            owner_user_id,
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
        if authority_uuid != self.entities.authority_uuid() {
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
        let stream_timer = if stream_appends.is_empty() { None } else {
            crate::fsync_stats::StageTimer::diagnostic(
                crate::fsync_stats::STAGE_AUTHORITY_COMMIT_STREAM_PREPARE)
        };
        let mut prepared_streams: Vec<PreparedStreamAppend> = Vec::new();
        for append in stream_appends {
            let committed_event_count =
                persisted_event_count(&self.entities, &mut entity, &append.key)?;
            let prepared = prepare_persisted_append(
                self.entities.writer_engine()?,
                &append.key,
                committed_event_count,
                append.expected_next_sequence,
                &append.events,
            )?;
            prepared.stage_persisted_catalog(&self.entities, &mut entity)?;
            prepared_streams.push(prepared);
        }
        drop(stream_timer);
        let prepared_entity = {
            let _timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_PREPARE);
            self.entities.prepare_commit(entity)?
        };

        if !private_blobs_published {
            if let Some(blobs) = prepared_blobs {
                let _timer = crate::fsync_stats::StageTimer::diagnostic(
                    crate::fsync_stats::STAGE_AUTHORITY_COMMIT_PRIVATE_BLOB_PUBLISH);
                blobs.publish(self.entities.writer_engine()?)?;
            }
        }

        let engine_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_ENGINE_COMMIT);
        let builder_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_BUILDER);
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
        // EntityOverlay is the highest family kind (5): it encodes last so the
        // envelope stays canonical when the same commit also publishes
        // stream/receipt/outbox records (kinds 2-4) — the failure shape behind
        // the 2026-09-19 differential conversation.delete protocol error.
        if let Some(overlay_record) = prepared_entity.overlay_record() {
            builder.add_record(
                FamilyRecordKind::EntityOverlay,
                overlay_record,
                std::iter::empty(),
            )?;
        }
        builder.add_block_references(staged_blob_block_ids)?;
        if prepared_entity.root_record().is_none()
            && prepared_entity.overlay_record().is_none()
            && prepared_streams.is_empty()
        {
            return Err(invalid_input(
                "authority transaction has no durable semantic mutation",
            ));
        }
        let envelope = builder.prepare()?;

        drop(builder_timer);
        let committed = self
            .entities
            .writer_engine_mut()?
            .commit_references_with_authority_state(
                &envelope.inline_payload,
                &envelope.block_ids,
                prepared_entity.authority_state_root(),
            )?;
        drop(engine_timer);
        let _apply_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_APPLY);
        self.entities
            .apply_prepared_commit(prepared_entity, committed.sequence)?;
        let stream_results = prepared_streams
            .iter()
            .map(|prepared| prepared.committed_result(committed.sequence))
            .collect();
        Ok(AuthorityCommitResult {
            conversation_notices: conversation_notices.into_iter()
                .map(|(key, sequence)| ConversationCommitNotice {
                    user_id: owner_user_id, key, sequence,
                }).collect(),
            transaction_sequence: committed.sequence,
            stream_appends: stream_results,
        })
    }
}

#[cfg(test)]
mod tests {
        #[test]
        fn authority_gc_defers_while_a_read_replica_pin_is_active() {
            let directory = tempfile::tempdir().unwrap();
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            database
                .entity_put(
                    &mut transaction,
                    crate::entity::EntityKey::new(7, 11, "conversation", b"k").unwrap(),
                    b"v".to_vec(),
                )
                .unwrap();
            database.commit(transaction).unwrap();

            let replica = database.build_read_replica().unwrap();
            let error = database
                .begin_garbage_collection(
                    crate::authority_gc::AuthorityGarbageCollectionBudget::conservative(),
                )
                .err()
                .unwrap();
            assert_eq!(error.kind(), io::ErrorKind::WouldBlock);
            drop(replica);
            database
                .begin_garbage_collection(
                    crate::authority_gc::AuthorityGarbageCollectionBudget::conservative(),
                )
                .unwrap()
                .finish()
                .unwrap();
        }

    use super::*;

    #[test]
    fn configure_write_path_budgets_applies_probe_derived_values() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database
            .configure_write_path_budgets(
                64 * 1024 * 1024,
                40 * 1024 * 1024,
                32 * 1024 * 1024,
                256 * 1024 * 1024,
            )
            .unwrap();
        assert_eq!(database.entities.overlay_budget_bytes, 64 * 1024 * 1024);
        assert_eq!(
            database.entities.overlay_wal_watermark_bytes,
            40 * 1024 * 1024
        );
        assert_eq!(
            database
                .entities
                .writer_engine()
                .unwrap()
                .checkpoint_materialize_batch_bytes,
            32 * 1024 * 1024
        );
        // Out-of-range values keep the consumer-side validation behavior.
        assert_eq!(
            database
                .configure_write_path_budgets(64 * 1024 * 1024, 40 * 1024 * 1024, 0, 0)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn conversation_notice_budget_coalesces_and_rejects_excess_keys() {
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        transaction.note_conversation_commit("one", 4).unwrap();
        transaction.note_conversation_commit("one", 2).unwrap();
        transaction.note_conversation_commit("one", 9).unwrap();
        assert_eq!(transaction.conversation_notices["one"], 9);
        for index in 1..crate::generated_storage_v2::MAX_COMMIT_NOTICE_KEYS {
            transaction
                .note_conversation_commit(&format!("key-{index}"), 1)
                .unwrap();
        }
        assert_eq!(
            transaction
                .note_conversation_commit("overflow", 1)
                .unwrap_err()
                .kind(),
            io::ErrorKind::OutOfMemory
        );
        transaction.note_conversation_commit("one", 10).unwrap();
        assert_eq!(
            transaction.conversation_notices.len(),
            crate::generated_storage_v2::MAX_COMMIT_NOTICE_KEYS
        );
        assert!(transaction
            .note_conversation_commit(
                &"x".repeat(crate::generated_storage_v2::MAX_COMMIT_NOTICE_KEY_BYTES + 1),
                1
            )
            .is_err());
    }


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
        let sequence = recovered.entities.writer_engine().unwrap().state().durable_sequence;
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
        let durable_sequence = recovered.entities.writer_engine().unwrap().state().durable_sequence;
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
    fn stream_demand_exceeds_former_count_and_byte_quotas_and_reopens_exactly() {
        let directory = tempfile::tempdir().unwrap();
        let large_key = stream_key(b"large-demand");
        let small_key = stream_key(b"small-demand");
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            for (start, end) in [(0, 2500), (2500, 5001)] {
                let events = (start..end).map(|ordinal| StreamEvent::new(
                    ordinal, "updated", vec![(ordinal % 251) as u8; 2048]).unwrap()).collect();
                database.stream_append(&mut transaction, large_key.clone(), start as u64 + 1, events).unwrap();
            }
            let small = (0..5001).map(|ordinal| StreamEvent::new(ordinal, "updated",
                vec![(ordinal % 251) as u8]).unwrap()).collect();
            database.stream_append(&mut transaction, small_key.clone(), 1, small).unwrap();
            let committed = database.commit(transaction).unwrap();
            assert_eq!(committed.stream_appends.len(), 2);
            assert!(committed.stream_appends.iter().all(|append| append.transaction_sequence == 1));
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        for (key, bytes) in [(large_key, 2048), (small_key, 1)] {
            let mut next = 1;
            let mut count = 0;
            loop {
                let page = database.stream_read(7, 11, &key, next, 997).unwrap();
                for row in &page.events {
                    assert_eq!(row.sequence, count + 1);
                    assert_eq!(row.event.payload, vec![(count % 251) as u8; bytes]);
                    count += 1;
                }
                if page.end_of_stream { break; }
                assert!(page.next_sequence > next);
                next = page.next_sequence;
            }
            assert_eq!(count, 5001);
        }
    }

    #[test]
    fn staged_blob_demand_exceeds_former_reference_quota_and_reopens_all_graphs() {
        let directory = tempfile::tempdir().unwrap();
        let mut references = Vec::new();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut transaction = database.begin(7, 11).unwrap();
            for ordinal in 0..2101_u64 {
                let payload = ordinal.to_be_bytes();
                let reference = database.stage_blob(&mut transaction,
                    &mut payload.as_slice(), payload.len() as u64).unwrap();
                // Persist every graph root in the same owning entity transaction.
                database.entity_put(&mut transaction, entity_key(&payload),
                    reference.manifest_block_id.0.to_vec()).unwrap();
                references.push(reference);
            }
            assert!(transaction.staged_blob_block_ids.len() > 2048);
            database.commit(transaction).unwrap();
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        for (ordinal, reference) in references.into_iter().enumerate() {
            let payload = (ordinal as u64).to_be_bytes();
            assert_eq!(database.entity_get(&mut transaction, &entity_key(&payload)).unwrap(),
                Some(reference.manifest_block_id.0.to_vec()));
            let mut reader = database.blob_reader(7, 11, reference).unwrap();
            assert_eq!(reader.next_chunk().unwrap().unwrap().as_slice(), payload);
            assert!(reader.next_chunk().unwrap().is_none());
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
            // Manual mid-test checkpoints strand overlay deltas below the
            // checkpoint boundary; this geometry test pins the legacy
            // synchronous write path.
            database.set_write_overlay_for_test(false);
            let mut first = database.begin(7, 11).unwrap();
            database
                .stream_append(&mut first, key.clone(), 1, vec![event(1)])
                .unwrap();
            database.commit(first).unwrap();
            database.entities.writer_engine_mut().unwrap().checkpoint().unwrap();
            let mut second = database.begin(7, 11).unwrap();
            database
                .stream_append(&mut second, key.clone(), 2, vec![event(2)])
                .unwrap();
            database.commit(second).unwrap();
            database.entities.writer_engine_mut().unwrap().checkpoint().unwrap();
            assert_eq!(
                database.entities.writer_engine().unwrap().history_segment_block_ids().len(),
                2
            );
            latest_history = database.entities.writer_engine().unwrap().history_segment_block_ids()[1];
            assert_eq!(
                database.entities.writer_engine().unwrap().state().authority_state_root,
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

        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();

        database.set_write_overlay_for_test(false);
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
        database.set_write_overlay_for_test(false);
        let mut append = database.begin(7, 11).unwrap();
        database
            .stream_append(&mut append, key.clone(), 1, vec![event(1), event(2)])
            .unwrap();
        database.commit(append).unwrap();
        crate::backup::create_incremental_backup(database.entities.writer_engine_mut().unwrap(), backup.path())
            .unwrap();
        let expected_root = database.entities.writer_engine().unwrap().state().authority_state_root;
        drop(database);

        let restored_manifest =
            crate::backup::restore_latest_backup(backup.path(), target.path()).unwrap();
        assert_eq!(restored_manifest.authority_state_root, expected_root);
        let mut restored = AuthorityDatabase::open(target.path()).unwrap();
        restored.set_write_overlay_for_test(false);
        assert_eq!(
            restored.entities.writer_engine().unwrap().state().authority_state_root,
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
                .writer_engine()
                .unwrap()
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
    fn automatic_history_compaction_yields_between_launch_sized_slices() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.set_write_overlay_for_test(false);
        database.engine_mut_for_test().set_checkpoint_materialize_batch_bytes(
            crate::history::HISTORY_SEGMENT_MAX_BYTES as u64).unwrap();
        for index in 0..5_u8 {
            let mut tx = database.begin(7, 11).unwrap();
            database.entity_put(&mut tx, entity_key(&[index]), vec![index; 64]).unwrap();
            database.commit(tx).unwrap();
            database.engine_mut_for_test().checkpoint().unwrap();
        }
        assert_eq!(database.engine_mut_for_test().history_segment_count(), 5);
        for remaining in (1..5).rev() {
            let prepared = database.try_prepare_history_compaction(1).unwrap().unwrap();
            let encoded = crate::engine::encode_history_compaction(&prepared);
            let progress = database.finish_history_compaction(prepared, encoded).unwrap();
            assert_eq!(progress.retired_segments, 1);
            assert_eq!(progress.retained_segments, remaining);
            // The physical lane is released between publications.
            assert!(database.engine_mut_for_test().require_write_admission().is_ok());
        }
        assert!(database.try_prepare_history_compaction(1).unwrap().is_none());
        drop(database);
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        let mut tx = reopened.begin(7, 11).unwrap();
        for index in 0..5_u8 {
            assert_eq!(reopened.entity_get(&mut tx, &entity_key(&[index])).unwrap(), Some(vec![index;64]));
        }
    }

    #[test]
    fn indexed_history_pruning_retains_pending_and_partial_overlay_transactions() {
        let vfs = Arc::new(DeterministicVfs::new(None));
        vfs.create_dir(Path::new("/data")).unwrap();
        vfs.sync_directory(Path::new("/")).unwrap();
        let mut database = AuthorityDatabase::initialize_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        database.set_write_overlay_for_test(true);
        for round in 0..3_u16 {
            let mut tx = database.begin(7, 11).unwrap();
            for index in 0..512_u16 {
                database.entity_put(&mut tx, entity_key(&(round * 512 + index).to_be_bytes()), vec![round as u8; 32]).unwrap();
            }
            database.commit(tx).unwrap();
            let engine = database.engine_mut_for_test();
            engine.attach_rotation_driver();
            let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
                crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
                other => panic!("expected rotation, got {other:?}"),
            };
            let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
            engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap();
        }
        assert_eq!(database.engine_mut_for_test().history_segment_count(), 3);
        assert!(database.try_prepare_history_compaction(1).unwrap().is_none());
        let first = database.entities.overlay_replay_first_sequence().unwrap();
        assert!(database.entities.merge_overlay().unwrap());
        assert_eq!(database.entities.overlay_replay_first_sequence().unwrap(), first,
            "partial sequence coverage must retain its boundary transaction");
        assert!(database.try_prepare_history_compaction(1).unwrap().is_none());
        // Crash before a full merge must recover every acknowledged value.
        std::mem::forget(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        for round in 0..3_u16 {
            for index in 0..512_u16 {
                assert_eq!(database.entity_get(&mut read, &entity_key(&(round * 512 + index).to_be_bytes())).unwrap(), Some(vec![round as u8; 32]));
            }
        }
        drop(read);
        database.entities.merge_overlay_fully().unwrap();
        let prepared = database.try_prepare_history_compaction(1).unwrap().unwrap();
        let outcome = crate::engine::encode_history_compaction(&prepared);
        let metrics = database.finish_history_compaction(prepared, outcome).unwrap();
        assert_eq!(metrics.retired_segments, 2);
        std::mem::forget(database);
        vfs.crash().unwrap();
        let mut database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs.clone()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        for round in 0..3_u16 {
            for index in 0..512_u16 {
                assert_eq!(database.entity_get(&mut read, &entity_key(&(round * 512 + index).to_be_bytes())).unwrap(), Some(vec![round as u8; 32]));
            }
        }
        drop(read);
        let mut tx = database.begin(7, 11).unwrap();
        database.entity_put(&mut tx, entity_key(b"after-prune"), b"new epoch".to_vec()).unwrap();
        database.commit(tx).unwrap();
        let engine = database.engine_mut_for_test();
        engine.attach_rotation_driver();
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected rotation, got {other:?}"),
        };
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap();
        std::mem::forget(database);
        vfs.crash().unwrap();
        let database = AuthorityDatabase::open_with_vfs(Path::new("/data"), vfs).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(database.entity_get(&mut read, &entity_key(b"after-prune")).unwrap(), Some(b"new epoch".to_vec()));
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
            // Manual mid-test checkpoints strand overlay deltas below the
            // checkpoint boundary; this receipt-geometry test pins the
            // legacy synchronous write path.
            database.set_write_overlay_for_test(false);
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
            database.entities.writer_engine_mut().unwrap().checkpoint().unwrap();
            for (name, value) in [
                (b"later-1".as_slice(), b"one".as_slice()),
                (b"later-2", b"two"),
            ] {
                let mut later = database.begin(7, 11).unwrap();
                database
                    .entity_put(&mut later, entity_key(name), value.to_vec())
                    .unwrap();
                database.commit(later).unwrap();
                database.entities.writer_engine_mut().unwrap().checkpoint().unwrap();
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
        database.set_write_overlay_for_test(false);
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
        assert_eq!(database.entities.writer_engine().unwrap().state().durable_sequence, 1);
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
                    .writer_engine()
                    .unwrap()
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
    fn large_outbox_uses_blob_and_retains_backlog_beyond_configured_hints() {
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
            .writer_engine()
            .unwrap()
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
        let mut second = database.begin(7, 11).unwrap();
        database.entity_put(&mut second, entity_key(b"second"), b"created".to_vec()).unwrap();
        database.logical_outbox_capture(&mut second, outbox_capture(payload, None)).unwrap();
        database.commit(second).unwrap();
        assert!(database.logical_outbox_status(7, 11).unwrap().pending_bytes > pending_bytes);
        let pending = database.logical_outbox_pending_bounded(7, 11, 10, 1).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].record.identity.sequence, 1);
        let mut inspect = database.begin(7, 11).unwrap();
        assert_eq!(database.entity_get(&mut inspect, &entity_key(b"second")).unwrap(), Some(b"created".to_vec()));

    }

    #[test]
    fn large_outbox_survives_reopen_publication_retry_and_exact_ack_beyond_hints() {
        use crate::outbox_publisher::{publish_batch, LogicalOutboxPublishBudget};
        use crate::outbox_sink::EngineLogicalOutboxSink;
        let directory = tempfile::tempdir().unwrap();
        let key = [18; 32];
        let payload = vec![37; 9 * 1024 * 1024];
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        database.configure_logical_outbox(&key, 1, 1).unwrap();
        let mut write = database.begin(7, 11).unwrap();
        database.entity_put(&mut write, entity_key(b"large-demand"), b"committed".to_vec()).unwrap();
        database.logical_outbox_capture(&mut write, outbox_capture(payload.clone(), Some("large-demand"))).unwrap();
        database.commit(write).unwrap();
        drop(database);
        let mut database = AuthorityDatabase::open(directory.path()).unwrap();
        database.configure_logical_outbox(&key, 0, 0).unwrap();
        let pending = database.logical_outbox_pending_bounded(7, 11, 1, 1).unwrap();
        assert_eq!(pending.len(), 1);
        assert_eq!(database.logical_outbox_decrypt(&pending[0].record).unwrap(), payload);
        let sink_path = tempfile::tempdir().unwrap();
        let mut sink = EngineLogicalOutboxSink::initialize(sink_path.path(), 7, 11, 1).unwrap();
        let started = std::time::Instant::now();
        let budget = LogicalOutboxPublishBudget::new(1, 1).unwrap();
        let published = publish_batch(&mut sink, &pending, budget).unwrap();
        let repeated = publish_batch(&mut sink, &pending, budget).unwrap();
        assert_eq!(published.receipts, repeated.receipts);
        let receipt = published.receipts[0];
        let acknowledged = database.logical_outbox_acknowledge(7, 11, receipt.sequence, receipt.event_id).unwrap();
        assert_eq!(acknowledged.pending_bytes, 0);
        assert_eq!(database.logical_outbox_acknowledge(7, 11, receipt.sequence, receipt.event_id).unwrap(), acknowledged);
        let mut read = database.begin(7, 11).unwrap();
        assert_eq!(database.entity_get(&mut read, &entity_key(b"large-demand")).unwrap(), Some(b"committed".to_vec()));
        eprintln!("outbox_large payload_bytes={} record_bytes={} publish_retry_us={} acknowledged_pending_bytes=0",
            payload.len(), pending[0].record_bytes, started.elapsed().as_micros());
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

#[cfg(test)]
mod shutdown_checkpoint_tests {
    use super::*;
    use std::io;

    #[test]
    fn shutdown_checkpoint_retires_the_active_wal_window() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();

        // An empty committed window is "nothing to retire", not a failure:
        // the daemon's shutdown tail treats InvalidInput as a clean skip.
        let error = database.checkpoint_wal_window().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);

        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(
                &mut transaction,
                crate::entity::EntityKey::new(7, 11, "conversation", b"k").unwrap(),
                b"v".to_vec(),
            )
            .unwrap();
        database.commit(transaction).unwrap();
        let before = database.entities.writer_engine().unwrap().wal_len();
        assert!(before > 0);

        database.merge_entity_overlay_fully().unwrap();
        database.checkpoint_wal_window().unwrap();
        let state = database.entities.writer_engine().unwrap().state();
        assert_eq!(state.durable_sequence, state.checkpoint_sequence);

        // The retired window stays retired: a second shutdown checkpoint is
        // again the clean "nothing to retire" skip.
        let error = database.checkpoint_wal_window().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }
}

#[cfg(test)]
#[path = "authority_stream_staging_tests.rs"]
mod stream_staging_tests;
