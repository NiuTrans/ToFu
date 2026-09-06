//! Bounded, owner-scoped Transaction IR and its sole authority interpreter.
//!
//! Semantic compilers produce this concrete IR after validating their wire
//! payload. The interpreter opens one OCC transaction, evaluates reads and
//! conditional writes in order, derives the response, and atomically attaches
//! the command receipt and logical outbox record. No compiler receives direct
//! access to physical pages, WAL records, or blob paths.

use std::io;

use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_storage_operations::StorageOperationKind;
pub use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS, MAX_TRANSACTION_IR_LITERAL_BYTES, MAX_TRANSACTION_IR_SLOTS,
    MAX_TRANSACTION_IR_STEPS,
};
use crate::generated_tofudb_ir::{
    MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS, MAX_MODEL_ROUTING_DOCUMENT_BYTES,
    MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS, MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY,
    MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS, MAX_MODEL_ROUTING_TIMESTAMP_SECONDS,
    MAX_PROVIDER_TENANT_LABEL_CHARACTERS, MAX_RECENT_PROJECT_PATH_CHARACTERS,
    MAX_RECENT_PROJECT_TOUCH_BATCH, MAX_TASK_RESULT_CACHE_FACT,
    MAX_TASK_RESULT_COST_EXPERIMENT_ROWS, MAX_TASK_RESULT_COST_EXPERIMENT_SCAN_ROWS,
    MAX_TASK_RESULT_DOCUMENT_BYTES, MAX_TASK_RESULT_RECOVERY_ROWS,
    MAX_TASK_RESULT_RECOVERY_SCAN_ROWS, MAX_TASK_RESULT_SUMMARY_ROWS,
    MAX_TASK_RESULT_SUMMARY_SCAN_ROWS, MAX_WORKER_JOB_CLAIM_KINDS,
    MAX_WORKER_JOB_CLOCK_MILLISECONDS, MAX_WORKER_JOB_ERROR_BYTES, MAX_WORKER_JOB_PAYLOAD_BYTES,
    MAX_WORKER_JOB_PRIORITY,
};
use crate::logical_outbox::LogicalOutboxCapture;
use crate::stream::{StreamEvent, StreamKey};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EntityWriteCondition {
    Always,
    SlotMissing(u8),
    SlotPresent(u8),
}

#[derive(Clone, Debug)]
pub struct IndexedStreamAppendItem {
    pub task_id: String,
    pub application_sequence: u64,
    pub event_type: String,
    pub payload_json: Vec<u8>,
    pub created_at_ms: i64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IndexedStreamRetentionClass {
    Streaming,
    Structural,
}

#[derive(Clone, Debug)]
pub enum TransactionIrStep {
    BillingLedgerAppend {
        entry: crate::billing::LedgerEntry,
        slot: u8,
    },
    BillingLedgerFind {
        user_id: String,
        kind: String,
        ref_type: String,
        ref_id: String,
        slot: u8,
    },
    BillingLedgerList {
        request: crate::billing::LedgerListRequest,
        slot: u8,
    },
    BillingLedgerRecompute {
        user_id: String,
        slot: u8,
    },
    BillingPaymentFind {
        provider: String,
        provider_id: String,
        slot: u8,
    },
    BillingPaymentList {
        request: crate::billing::PaymentListRequest,
        slot: u8,
    },
    BillingPaymentRecord {
        provider: String,
        provider_id: String,
        payload_json: Vec<u8>,
        created_at: u64,
        physical_updated_at_ms: u64,
        slot: u8,
    },
    BillingPaymentSettle {
        payment_id: String,
        payload_json: Vec<u8>,
        settled_at: u64,
        physical_updated_at_ms: u64,
        slot: u8,
    },
    BillingRedeemCodeApply {
        request: crate::billing::RedeemApplyRequest,
        slot: u8,
    },
    BillingRedeemCodesList {
        request: crate::billing::RedeemListRequest,
        slot: u8,
    },
    BillingRedeemCodesMint {
        request: crate::billing::RedeemMintRequest,
        slot: u8,
    },
    BillingReserveStale {
        cutoff_ts: u64,
        limit: usize,
        slot: u8,
    },
    BillingWalletApply {
        request: crate::billing::WalletApplyRequest,
        slot: u8,
    },
    BillingWalletGet {
        user_id: String,
        slot: u8,
    },
    BillingWalletSettle {
        request: crate::billing::WalletSettleRequest,
        slot: u8,
    },
    ArtifactCreate {
        request: crate::artifact::CreateRequest,
        slot: u8,
    },
    ArtifactGet {
        artifact_id: String,
        include_content: bool,
        slot: u8,
    },
    ArtifactList {
        conversation_id: String,
        include_deleted: bool,
        slot: u8,
    },
    ArtifactDelete {
        artifact_id: String,
        deleted_at_ms: u64,
        slot: u8,
    },
    ArtifactVersions {
        artifact_id: String,
        slot: u8,
    },
    ArtifactLibrary {
        limit: usize,
        slot: u8,
    },
    ArtifactPin {
        artifact_id: String,
        pinned: bool,
        slot: u8,
    },
    ToolResultArtifactPut {
        content: String,
        media_type: String,
        created_at_ms: u64,
        expires_at_ms: u64,
        slot: u8,
    },
    ToolResultArtifactRead {
        artifact_ref: String,
        now_ms: u64,
        offset: usize,
        limit: usize,
        slot: u8,
    },
    ToolResultArtifactSearch {
        artifact_ref: String,
        query: String,
        now_ms: u64,
        cursor: usize,
        limit: usize,
        slot: u8,
    },
    ToolResultArtifactPrune {
        now_ms: u64,
        limit: usize,
        slot: u8,
    },
    SystemSchemaVersion {
        slot: u8,
    },
    RateLimitRecordAndCheck {
        request: crate::rate_limit::RecordAndCheckRequest,
        slot: u8,
    },
    CompactionArchiveCreate {
        archive_id: String,
        conversation_id: String,
        messages_json: Vec<u8>,
        summary: String,
        receipt_json: Vec<u8>,
        trigger: String,
        task_id: String,
        round_num: u64,
        model: String,
        tokens_before: u64,
        tokens_after: u64,
        msgs_before: u64,
        msgs_after: u64,
        reason: String,
        created_at_ms: u64,
        committed_at_ms: u64,
        slot: u8,
    },
    CompactionArchiveList {
        conversation_id: String,
        limit: usize,
        slot: u8,
    },
    CompactionArchiveGet {
        conversation_id: String,
        archive_id: String,
        include_messages: bool,
        slot: u8,
    },
    CompactionArchiveUpdateSummary {
        archive_id: String,
        summary: String,
        tokens_after: u64,
        msgs_after: u64,
        receipt_json: Option<Vec<u8>>,
        committed_at_ms: u64,
        slot: u8,
    },
    CompactionArchiveDeleteConversation {
        conversation_id: String,
        slot: u8,
    },
    CompactionArchivePrune {
        conversation_id: String,
        keep: usize,
        slot: u8,
    },
    ConversationCreate {
        conversation_id: String,
        title: String,
        settings_json: Vec<u8>,
        created_at_ms: u64,
        updated_at_ms: u64,
        committed_at_ms: u64,
    },
    ConversationClone {
        source_conversation_id: String,
        destination_conversation_id: String,
        title: Option<String>,
        identity_seed: [u8; 32],
        committed_at_ms: u64,
        slot: u8,
    },
    ConversationDelete {
        conversation_id: String,
        deleted_at_ms: u64,
        slot: u8,
    },
    ConversationRestore {
        conversation_id: String,
        committed_at_ms: u64,
        slot: u8,
    },
    ConversationPurge {
        conversation_id: String,
        purged_at_ms: u64,
        slot: u8,
    },
    ConversationTrashPrune {
        deleted_before_ms: u64,
        maximum_conversations: usize,
        slot: u8,
    },
    ConversationCount {
        slot: u8,
    },
    ConversationActivityDates {
        updated_at_gte: i64,
        created_at_lt: Option<i64>,
        day_boundaries_ms: Vec<i64>,
        limit: usize,
        slot: u8,
    },
    ConversationGet {
        conversation_id: String,
        include_messages: bool,
        message_window: usize,
        before_sequence: Option<i64>,
        slot: u8,
    },
    ConversationSettingsUpdate {
        conversation_id: String,
        updates_json: Vec<u8>,
        replace: bool,
        expected_settings_json: Option<Vec<u8>>,
        expected_revision: Option<u64>,
        committed_at_ms: u64,
        slot: u8,
    },
    ConversationMetadataUpdate {
        conversation_id: String,
        title: Option<String>,
        updated_at_ms: Option<u64>,
        committed_at_ms: u64,
        slot: u8,
    },
    ConversationCatalogPage {
        folder_id: Option<String>,
        before_updated_at_ms: Option<u64>,
        before_id: String,
        limit: usize,
        settings_keys: Option<Vec<String>>,
        slot: u8,
    },
    ConversationList {
        project_path: Option<String>,
        title_contains: Option<String>,
        ids: Option<std::collections::BTreeSet<String>>,
        updated_at_gte: Option<i64>,
        updated_at_gt: Option<i64>,
        created_at_lt: Option<i64>,
        updated_descending: bool,
        settings_keys: Option<Vec<String>>,
        include_messages: bool,
        limit: usize,
        slot: u8,
    },
    ModelRoutingGet {
        tenant_label: String,
        slot: u8,
    },
    ModelRoutingCommit {
        tenant_label: String,
        expected_revision: u64,
        document_json: Vec<u8>,
        migration_receipt_json: Option<Vec<u8>>,
        updated_at: f64,
        slot: u8,
    },
    ModelRoutingMigrationReceiptGet {
        tenant_label: String,
        slot: u8,
    },
    ModelRoutingMigrationReceiptPut {
        tenant_label: String,
        receipt_json: Vec<u8>,
        initial_document_json: Option<Vec<u8>>,
        updated_at: f64,
        slot: u8,
    },
    ModelRoutingSecretPut {
        tenant_label: String,
        secret_reference: String,
        ciphertext: String,
        key_hint: String,
        updated_at: f64,
        slot: u8,
    },
    ModelRoutingSecretGet {
        tenant_label: String,
        secret_reference: String,
        slot: u8,
    },
    ModelRoutingSecretList {
        tenant_label: String,
        slot: u8,
    },
    ModelRoutingSecretDelete {
        tenant_label: String,
        secret_reference: String,
        slot: u8,
    },
    ModelRoutingSecretPrune {
        tenant_label: String,
        active_secret_references: std::collections::BTreeSet<String>,
        updated_before: f64,
        slot: u8,
    },
    ProviderCreate {
        tenant_label: String,
        provider_id: String,
        document_json: Vec<u8>,
        physical_updated_at_ms: u64,
        slot: u8,
    },
    ProviderGet {
        tenant_label: String,
        provider_id: String,
        slot: u8,
    },
    ProviderList {
        tenant_label: String,
        slot: u8,
    },
    ProviderUpdate {
        tenant_label: String,
        provider_id: String,
        updates_json: Vec<u8>,
        updated_at: f64,
        physical_updated_at_ms: u64,
        slot: u8,
    },
    ProviderDelete {
        tenant_label: String,
        provider_id: String,
        slot: u8,
    },
    ProviderTouch {
        tenant_label: String,
        provider_id: String,
        used_at: f64,
        physical_updated_at_ms: u64,
        slot: u8,
    },
    TaskResultCheckpoint {
        task_id: String,
        value_json: Vec<u8>,
        expected_version: u64,
        updated_at_ms: u64,
        guarded: bool,
        require_parent: bool,
        cache_prefix_hwm: Option<u64>,
        last_turn_cache_read: Option<u64>,
        slot: u8,
    },
    TaskResultReplayGet {
        task_id: String,
        requested_user_id: u64,
        include_terminal_payload: bool,
        include_metadata: bool,
        slot: u8,
    },
    TaskResultSummaryList {
        requested_user_id: Option<u64>,
        status: Option<String>,
        conversation_id: Option<String>,
        completed_before_ms: Option<i64>,
        limit: usize,
        scan_limit: usize,
        order_by: String,
        after_key: String,
        slot: u8,
    },
    TaskResultAbort {
        task_id: String,
        requested_user_id: u64,
        source: String,
        requested_at_ms: u64,
        slot: u8,
    },
    TaskResultAbortRequested {
        task_id: String,
        requested_user_id: u64,
        slot: u8,
    },
    TaskResultRecoverRunning {
        interrupted_reason: String,
        maximum_rows: usize,
        scan_limit: usize,
        updated_at_ms: u64,
        slot: u8,
    },
    TaskResultCostExperimentScan {
        requested_user_id: u64,
        experiment_id: String,
        completed_at_gte: i64,
        limit: usize,
        scan_limit: usize,
        after_key: String,
        slot: u8,
    },
    TenantUserCreate {
        request: crate::tenant_user::CreateRequest,
        slot: u8,
    },
    TenantUserGet {
        selector: crate::tenant_user::Selector,
        slot: u8,
    },
    TenantUserList {
        request: crate::tenant_user::ListRequest,
        slot: u8,
    },
    TenantUserSetStatus {
        user_id: String,
        status: String,
        slot: u8,
    },
    TenantUserSetRole {
        user_id: String,
        role: String,
        slot: u8,
    },
    TenantUserAuthentication {
        email: String,
        slot: u8,
    },
    TenantUserRecordLogin {
        user_id: String,
        last_login_at: u64,
        slot: u8,
    },
    CredentialCreate {
        request: crate::credential::CreateRequest,
        slot: u8,
    },
    CredentialCreateIfOwnerEmpty {
        request: crate::credential::CreateRequest,
        slot: u8,
    },
    CredentialGet {
        boundary: crate::credential::Boundary,
        credential_id: String,
        slot: u8,
    },
    CredentialList {
        boundary: crate::credential::Boundary,
        slot: u8,
    },
    CredentialExists {
        boundary: crate::credential::Boundary,
        slot: u8,
    },
    CredentialAuthenticate {
        secret_hash: String,
        now: f64,
        slot: u8,
    },
    CredentialValidate {
        secret_hash: String,
        now: f64,
        slot: u8,
    },
    CredentialIdentify {
        secret_hash: String,
        slot: u8,
    },
    CredentialTouch {
        boundary: crate::credential::Boundary,
        credential_id: String,
        used_at: f64,
        touch_if_before: f64,
        slot: u8,
    },
    CredentialUpdate {
        boundary: crate::credential::Boundary,
        credential_id: String,
        request: crate::credential::UpdateRequest,
        slot: u8,
    },
    CredentialRevoke {
        boundary: crate::credential::Boundary,
        credential_id: String,
        revoked_at: f64,
        slot: u8,
    },
    WorkerJobEnqueue {
        task_id: String,
        user_id: u64,
        tenant_label: String,
        task_kind: String,
        payload_json: Vec<u8>,
        idempotency_key: String,
        request_digest: String,
        priority: u16,
        available_at_ms: u64,
        now_ms: u64,
        slot: u8,
    },
    WorkerJobGet {
        task_id: String,
        user_id: u64,
        slot: u8,
    },
    WorkerJobClaimNext {
        worker_id: String,
        now_ms: u64,
        lease_ms: u64,
        task_kinds: Vec<String>,
        slot: u8,
    },
    WorkerJobHeartbeat {
        task_id: String,
        worker_id: String,
        fencing_token: u64,
        now_ms: u64,
        lease_ms: u64,
        replay_cursor: u64,
        slot: u8,
    },
    WorkerJobClaimState {
        task_id: String,
        worker_id: String,
        fencing_token: u64,
        now_ms: u64,
        slot: u8,
    },
    WorkerJobRequestCancel {
        task_id: String,
        user_id: u64,
        now_ms: u64,
        reason: String,
        slot: u8,
    },
    WorkerJobComplete {
        task_id: String,
        worker_id: String,
        fencing_token: u64,
        now_ms: u64,
        terminal_status: String,
        result_ref: String,
        replay_cursor: u64,
        error_json: Vec<u8>,
        slot: u8,
    },
    TurnAppendSettled {
        conversation_id: String,
        actor: String,
        status: String,
        projection_json: Vec<u8>,
        settlement_json: Vec<u8>,
        lane_id: String,
        command_id: String,
        kind: String,
        run_id: String,
        turn_id: String,
        attempt_id: Option<String>,
        created_at_ms: u64,
        committed_at_ms: u64,
        allow_create: bool,
        default_title: String,
        default_settings_json: Vec<u8>,
        default_created_at_ms: u64,
        slot: u8,
    },
    TurnCreatePair {
        request: Box<crate::turn::CreatePairRequest>,
        slot: u8,
    },
    TurnQueueActivate {
        request: crate::turn::QueueTransitionRequest,
        slot: u8,
    },
    TurnQueueCancel {
        request: crate::turn::QueueTransitionRequest,
        slot: u8,
    },
    TurnSteerCommit {
        request: crate::turn::SteerCommitRequest,
        slot: u8,
    },
    TurnRelatedAnnounce {
        request: crate::turn::RelatedAnnounceRequest,
        slot: u8,
    },
    TurnVisibleSync {
        request: Box<crate::turn::VisibleSyncRequest>,
        slot: u8,
    },
    TurnExists {
        conversation_id: String,
        slot: u8,
    },
    TurnGet {
        conversation_id: String,
        turn_id: String,
        slot: u8,
    },
    TurnImageGet {
        conversation_id: String,
        turn_id: String,
        projection_revision: u64,
        image_index: usize,
        slot: u8,
    },
    TurnList {
        conversation_id: String,
        lane_id: Option<String>,
        slot: u8,
    },
    TurnListDelta {
        conversation_id: String,
        since_ms: u64,
        known_revisions: std::collections::BTreeMap<String, u64>,
        server_now_ms: u64,
        slot: u8,
    },
    TurnRevision {
        conversation_id: String,
        slot: u8,
    },
    TurnTimingTraceGet {
        task_id: String,
        slot: u8,
    },
    TurnTimingTraceList {
        conversation_id: String,
        before_created_at: Option<u64>,
        limit: usize,
        slot: u8,
    },
    TurnPerceptionRecord {
        request: crate::turn::PerceptionRecordRequest,
        slot: u8,
    },
    TurnRecover {
        request: crate::turn::RecoverRequest,
        slot: u8,
    },
    TurnEventRecord {
        request: crate::turn::EventRecordRequest,
        slot: u8,
    },
    TurnAttemptCreate {
        request: crate::turn::AttemptCreateRequest,
        slot: u8,
    },
    TurnAttemptDispatchableList {
        created_before_ms: u64,
        limit: usize,
        slot: u8,
    },
    TurnAttemptDispatchWorker {
        request: crate::turn::AttemptDispatchWorkerRequest,
        slot: u8,
    },
    TurnAttemptGet {
        attempt_id: String,
        slot: u8,
    },
    TurnAttemptClaim {
        attempt_id: String,
        dispatch_owner_id: String,
        committed_at_ms: u64,
        slot: u8,
    },
    TurnAttemptBind {
        attempt_id: String,
        task_id: String,
        dispatch_owner_id: String,
        committed_at_ms: u64,
        slot: u8,
    },
    TurnAttemptStart {
        attempt_id: String,
        task_id: String,
        committed_at_ms: u64,
        slot: u8,
    },
    TurnEventsList {
        attempt_id: String,
        after: u64,
        limit: usize,
        patch_mode: bool,
        slot: u8,
    },
    TurnEventsPrune {
        request: crate::turn::EventPruneRequest,
        slot: u8,
    },
    TurnDelete {
        conversation_id: String,
        turn_ids: Vec<String>,
        deleted_at_ms: u64,
        slot: u8,
    },
    TurnCompact {
        request: crate::turn::CompactRequest,
        slot: u8,
    },
    TurnBranchCreate {
        conversation_id: String,
        parent_turn_id: String,
        lane_id: String,
        title: String,
        kind: String,
        anchor_text: String,
        parent_selection: String,
        expected_projection_revision: u64,
        updated_at_ms: u64,
        committed_at_ms: u64,
        slot: u8,
    },
    TurnBranchDelete {
        conversation_id: String,
        parent_turn_id: String,
        lane_id: String,
        deleted_at_ms: u64,
        committed_at_ms: u64,
        slot: u8,
    },
    TurnProjectionUpdate {
        conversation_id: String,
        turn_id: String,
        projection_json: Vec<u8>,
        expected_projection_revision: u64,
        updated_at_ms: u64,
        committed_at_ms: u64,
        slot: u8,
    },
    TurnSyncSnapshot {
        conversation_id: String,
        turn_limit: usize,
        include_artifact_hint: bool,
        slot: u8,
    },
    TurnSyncPage {
        conversation_id: String,
        lane_id: String,
        before_ordinal: u64,
        limit: usize,
        sync_sequence: u64,
        slot: u8,
    },
    TurnSyncChanges {
        conversation_id: String,
        after: u64,
        limit: usize,
        slot: u8,
    },
    TurnSyncPrune {
        request: crate::turn::SyncPruneRequest,
        slot: u8,
    },
    EntityRead {
        key: EntityKey,
        slot: u8,
    },
    EntityPut {
        key: EntityKey,
        value: Vec<u8>,
        condition: EntityWriteCondition,
    },
    EntityDelete {
        key: EntityKey,
        condition: EntityWriteCondition,
    },
    VersionedDocumentGet {
        key: EntityKey,
        namespace: String,
        logical_key: String,
        slot: u8,
    },
    VersionedDocumentList {
        start: EntityKey,
        end: EntityKey,
        namespace: String,
        limit: usize,
        slot: u8,
    },
    VersionedDocumentPut {
        key: EntityKey,
        namespace: String,
        logical_key: String,
        value_json: Vec<u8>,
        expected_version: Option<u64>,
        updated_at_ms: u64,
        slot: u8,
    },
    VersionedDocumentDelete {
        key: EntityKey,
        namespace: String,
        logical_key: String,
        expected_version: Option<u64>,
        slot: u8,
    },
    RecentProjectList {
        slot: u8,
    },
    RecentProjectTouch {
        path: String,
        last_used: u64,
        updated_at_ms: u64,
        slot: u8,
    },
    RecentProjectTouchMany {
        paths: Vec<String>,
        last_used: u64,
        updated_at_ms: u64,
        slot: u8,
    },
    RecentProjectClear {
        slot: u8,
    },
    ProjectRelink {
        old_path: String,
        new_path: String,
        updated_at_ms: u64,
        slot: u8,
    },
    Queue {
        request: crate::queue::Request,
        slot: u8,
    },
    Orchestration {
        request: crate::orchestration::Request,
        slot: u8,
    },
    IntegrationWorkspace {
        request: crate::integration_workspace::Request,
        slot: u8,
    },
    Swarm {
        request: crate::swarm::Request,
        slot: u8,
    },
    Scheduler {
        request: crate::scheduler::Request,
        slot: u8,
    },
    Timer {
        request: crate::timer::Request,
        slot: u8,
    },
    DailyCostMonth {
        year: u64,
        month: u64,
        slot: u8,
    },
    DailyCostLatest {
        slot: u8,
    },
    DailyCostPersistedDates {
        dates: Vec<String>,
        slot: u8,
    },
    DailyCostUpsert {
        date: String,
        cost: f64,
        conversations_json: Vec<u8>,
        computed_at: u64,
        updated_at_ms: u64,
        slot: u8,
    },
    DailyCostDelete {
        date: Option<String>,
        slot: u8,
    },
    LogAggregateFlush {
        rows_json: Vec<u8>,
        cutoff_ms: Option<u64>,
        updated_at_ms: u64,
        slot: u8,
    },
    LogAggregateQuery {
        level: String,
        q: String,
        sort: crate::log_aggregate::QuerySort,
        limit: usize,
        slot: u8,
    },
    PluginRegister {
        manifest_json: Vec<u8>,
        updated_at_ms: u64,
        slot: u8,
    },
    PluginManifestGet {
        namespace: String,
        slot: u8,
    },
    Paper {
        request: crate::paper::Request,
        slot: u8,
    },
    PaperArtifact {
        request: crate::paper_artifact::Request,
        slot: u8,
    },
    PaperLibrary {
        request: crate::paper_library::Request,
        slot: u8,
    },
    PaperPodcast {
        request: crate::paper_podcast::Request,
        slot: u8,
    },
    RawArchive {
        request: crate::raw_archive::Request,
        slot: u8,
    },
    Research {
        request: crate::research::ResearchRequest,
        slot: u8,
    },
    Optimizer {
        request: crate::optimizer::Request,
        slot: u8,
    },
    Knowledge {
        request: crate::knowledge::KnowledgeRequest,
        slot: u8,
    },
    ProjectBrainActiveList {
        slot: u8,
    },
    ProjectBrainRecoverySnapshot {
        slot: u8,
    },
    ProjectBrainGet {
        project_key: String,
        slot: u8,
    },
    ProjectBrainCommand {
        project_key: String,
        action: crate::project_brain::CommandAction,
        payload_json: Vec<u8>,
        timestamp: u64,
        slot: u8,
    },
    ProjectBrainRebuild {
        project_key: String,
        timestamp: u64,
        slot: u8,
    },
    BrowserSiteObservationGet {
        origin: String,
        route_family: String,
        operation: String,
        now_ms: u64,
        slot: u8,
    },
    BrowserSiteObservationRecord {
        origin: String,
        route_family: String,
        operation: String,
        outcome: String,
        observed_at_ms: u64,
        observation_json: Option<Vec<u8>>,
        committed_at_ms: u64,
        slot: u8,
    },
    StreamAppend {
        key: StreamKey,
        expected_next_sequence: u64,
        events: Vec<StreamEvent>,
    },
    IndexedStreamAppend {
        task_id: String,
        application_sequence: u64,
        event_type: String,
        payload_json: Vec<u8>,
        created_at_ms: i64,
        slot: u8,
    },
    IndexedStreamAppendBatch {
        items: Vec<IndexedStreamAppendItem>,
        slot: u8,
    },
    IndexedStreamList {
        task_id: String,
        after_sequence: Option<u64>,
        limit: usize,
        types: Vec<String>,
        type_prefixes: Vec<String>,
        slot: u8,
    },
    IndexedStreamInspectorSummary {
        root_task_ids: Vec<String>,
        slot: u8,
    },
    IndexedStreamPrune {
        created_before_ms: i64,
        limit: usize,
        retention_class: IndexedStreamRetentionClass,
        slot: u8,
    },
    IndexedStreamBounds {
        task_id: String,
        slot: u8,
    },
    IndexedStreamLatest {
        task_id: String,
        slot: u8,
    },
}

#[derive(Clone, Debug)]
pub enum TransactionIrResult {
    Literal(Vec<u8>),
    SlotOrLiteral { slot: u8, missing: Vec<u8> },
}

#[derive(Clone, Debug)]
pub struct TransactionIrCommandEffects {
    pub command_id: String,
    pub request_digest: [u8; 32],
    pub request_id: String,
    pub schema_version: u32,
    pub registry_version: u32,
    pub committed_at_ms: u64,
    pub outbox_payload: Vec<u8>,
    pub store_receipt: bool,
}

#[derive(Clone, Debug)]
pub struct TransactionIr {
    pub tenant_id: u64,
    pub owner_user_id: u64,
    pub operation: &'static str,
    pub operation_kind: StorageOperationKind,
    pub steps: Vec<TransactionIrStep>,
    pub result: TransactionIrResult,
    pub command_effects: Option<TransactionIrCommandEffects>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn validate_provider_step_slot(
    initialized_slots: &mut [bool; MAX_TRANSACTION_IR_SLOTS],
    slot: u8,
    tenant_label: &str,
    provider_id: Option<&str>,
) -> io::Result<()> {
    let initialized = initialized_slots
        .get_mut(slot as usize)
        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
    if *initialized {
        return Err(invalid_input("Transaction IR initializes a slot twice"));
    }
    *initialized = true;
    if tenant_label.chars().count() > MAX_PROVIDER_TENANT_LABEL_CHARACTERS
        || provider_id.is_some_and(|value| value.is_empty() || value.chars().count() > 128)
    {
        return Err(invalid_input("invalid provider identity step"));
    }
    Ok(())
}

fn validate_model_routing_slot(
    initialized_slots: &mut [bool; MAX_TRANSACTION_IR_SLOTS],
    slot: u8,
    tenant_label: &str,
) -> io::Result<()> {
    let initialized = initialized_slots
        .get_mut(slot as usize)
        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
    if *initialized {
        return Err(invalid_input("Transaction IR initializes a slot twice"));
    }
    *initialized = true;
    if tenant_label.chars().count() > MAX_PROVIDER_TENANT_LABEL_CHARACTERS {
        return Err(invalid_input("invalid model-routing tenant label"));
    }
    Ok(())
}

fn valid_model_routing_json_object(bytes: &[u8]) -> bool {
    bytes.len() <= MAX_MODEL_ROUTING_DOCUMENT_BYTES
        && serde_json::from_slice::<serde_json::Value>(bytes)
            .ok()
            .is_some_and(|value| value.is_object())
}

fn validate_worker_job_slot(
    initialized_slots: &mut [bool; MAX_TRANSACTION_IR_SLOTS],
    slot: u8,
) -> io::Result<()> {
    let initialized = initialized_slots
        .get_mut(slot as usize)
        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
    if *initialized {
        return Err(invalid_input("Transaction IR initializes a slot twice"));
    }
    *initialized = true;
    Ok(())
}

fn valid_worker_job_text(value: &str, maximum: usize, allow_empty: bool) -> bool {
    (allow_empty || !value.is_empty()) && value.chars().count() <= maximum
}

fn valid_credential_boundary(boundary: &crate::credential::Boundary) -> bool {
    boundary.owner_user_id > 0 && boundary.tenant_label.chars().count() <= 256
}

fn valid_credential_id(value: &str) -> bool {
    !value.is_empty() && value.chars().count() <= 128
}

fn valid_credential_secret(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn valid_credential_timestamp(value: f64) -> bool {
    value.is_finite()
        && (0.0..=crate::generated_tofudb_ir::MAX_CREDENTIAL_TIMESTAMP_SECONDS).contains(&value)
}

fn valid_billing_text(value: &str, maximum: usize, required: bool) -> bool {
    (!required || !value.is_empty()) && value.chars().count() <= maximum
}

fn valid_billing_kind(value: &str) -> bool {
    matches!(
        value,
        "topup"
            | "redeem"
            | "bonus"
            | "refund"
            | "adjust_credit"
            | "reserve"
            | "reserve_release"
            | "debit"
            | "adjust_debit"
    )
}

fn literal_bytes(result: &TransactionIrResult) -> usize {
    match result {
        TransactionIrResult::Literal(value) => value.len(),
        TransactionIrResult::SlotOrLiteral { missing, .. } => missing.len(),
    }
}

fn referenced_slot(condition: EntityWriteCondition) -> Option<u8> {
    match condition {
        EntityWriteCondition::Always => None,
        EntityWriteCondition::SlotMissing(slot) | EntityWriteCondition::SlotPresent(slot) => {
            Some(slot)
        }
    }
}

impl TransactionIr {
    pub fn validate(&self) -> io::Result<()> {
        if self.tenant_id == 0
            || self.owner_user_id == 0
            || self.operation.is_empty()
            || self.steps.len() > MAX_TRANSACTION_IR_STEPS
            || literal_bytes(&self.result) > MAX_TRANSACTION_IR_LITERAL_BYTES
        {
            return Err(invalid_input("invalid Transaction IR envelope"));
        }
        let mut initialized_slots = [false; MAX_TRANSACTION_IR_SLOTS];
        let mut literal_bytes = literal_bytes(&self.result);
        let mut task_result_bytes = 0_usize;
        for step in &self.steps {
            match step {
                TransactionIrStep::BillingLedgerAppend { entry, .. }
                    if !valid_billing_text(&entry.id, 200, true)
                        || !valid_billing_text(&entry.user_id, 200, true)
                        || !valid_billing_kind(&entry.kind)
                        || !valid_billing_text(&entry.ref_type, 100, false)
                        || !valid_billing_text(&entry.ref_id, 300, false)
                        || !valid_billing_text(
                            &entry.note,
                            crate::generated_tofudb_ir::MAX_BILLING_NOTE_CHARACTERS,
                            false,
                        ) =>
                {
                    return Err(invalid_input("invalid billing ledger append step"));
                }
                TransactionIrStep::BillingLedgerFind {
                    user_id,
                    kind,
                    ref_type,
                    ref_id,
                    ..
                } if !valid_billing_text(user_id, 200, true)
                    || !valid_billing_text(kind, 64, true)
                    || !valid_billing_text(ref_type, 100, false)
                    || !valid_billing_text(ref_id, 300, false) =>
                {
                    return Err(invalid_input("invalid billing ledger find step"));
                }
                TransactionIrStep::BillingLedgerList { request, .. }
                    if !valid_billing_text(&request.user_id, 200, true)
                        || !(1..=crate::generated_tofudb_ir::MAX_BILLING_LIST_ROWS)
                            .contains(&request.limit)
                        || request.offset.checked_add(request.limit).is_none_or(|v| {
                            v > crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS
                        })
                        || request.kinds.iter().any(|kind| !valid_billing_kind(kind)) =>
                {
                    return Err(invalid_input("invalid billing ledger list step"));
                }
                TransactionIrStep::BillingLedgerRecompute { user_id, .. }
                | TransactionIrStep::BillingWalletGet { user_id, .. }
                    if !valid_billing_text(user_id, 200, true) =>
                {
                    return Err(invalid_input("invalid billing user step"));
                }
                TransactionIrStep::BillingPaymentFind {
                    provider,
                    provider_id,
                    ..
                } if !valid_billing_text(provider, 100, true)
                    || !valid_billing_text(provider_id, 300, true) =>
                {
                    return Err(invalid_input("invalid billing payment find step"));
                }
                TransactionIrStep::BillingPaymentList { request, .. }
                    if !valid_billing_text(&request.user_id, 200, false)
                        || !valid_billing_text(&request.provider, 200, false)
                        || !valid_billing_text(&request.status, 200, false)
                        || !(1..=crate::generated_tofudb_ir::MAX_BILLING_LIST_ROWS)
                            .contains(&request.limit)
                        || request.offset > 10_000_000
                        || request
                            .offset
                            .checked_add(request.limit)
                            .is_none_or(|value| {
                                value > crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS
                            }) =>
                {
                    return Err(invalid_input("invalid billing payment list step"));
                }
                TransactionIrStep::BillingPaymentRecord {
                    provider,
                    provider_id,
                    payload_json,
                    ..
                } if !valid_billing_text(provider, 100, true)
                    || !valid_billing_text(provider_id, 300, true)
                    || payload_json.is_empty()
                    || payload_json.len()
                        > crate::generated_tofudb_ir::MAX_BILLING_PAYMENT_DOCUMENT_BYTES =>
                {
                    return Err(invalid_input("invalid billing payment record step"));
                }
                TransactionIrStep::BillingPaymentSettle {
                    payment_id,
                    payload_json,
                    ..
                } if !valid_billing_text(payment_id, 200, true)
                    || payload_json.is_empty()
                    || payload_json.len()
                        > crate::generated_tofudb_ir::MAX_BILLING_PAYMENT_DOCUMENT_BYTES =>
                {
                    return Err(invalid_input("invalid billing payment settle step"));
                }
                TransactionIrStep::BillingRedeemCodeApply { request, .. }
                    if !valid_billing_text(&request.code, 64, true)
                        || !valid_billing_text(&request.user_id, 200, true)
                        || !valid_billing_text(&request.ledger_id, 200, true)
                        || request.redeemed_at > i64::MAX as u64 =>
                {
                    return Err(invalid_input("invalid billing redeem apply step"));
                }
                TransactionIrStep::BillingRedeemCodesList { request, .. }
                    if !valid_billing_text(&request.batch, 80, false)
                        || !matches!(
                            request.status.as_str(),
                            "all" | "redeemed" | "unredeemed"
                        )
                        || !(1..=crate::generated_tofudb_ir::MAX_BILLING_LIST_ROWS)
                            .contains(&request.limit)
                        || request.offset > 10_000_000
                        || request
                            .offset
                            .checked_add(request.limit)
                            .is_none_or(|value| {
                                value > crate::generated_tofudb_ir::MAX_BILLING_LIST_SCAN_ROWS
                            }) =>
                {
                    return Err(invalid_input("invalid billing redeem list step"));
                }
                TransactionIrStep::BillingRedeemCodesMint { request, .. }
                    if !(1..=crate::generated_tofudb_ir::MAX_BILLING_REDEEM_CODES_PER_MINT)
                        .contains(&request.codes.len())
                        || !(1..=10_000_000_000_000).contains(&request.amount_micro)
                        || !valid_billing_text(&request.batch, 80, true)
                        || !valid_billing_text(&request.created_by, 200, false)
                        || !valid_billing_text(&request.note, 200, false)
                        || request.created_at > i64::MAX as u64
                        || request.expires_at > i64::MAX as u64
                        || request
                            .codes
                            .iter()
                            .any(|code| !valid_billing_text(code, 64, true))
                        || request
                            .codes
                            .iter()
                            .collect::<std::collections::BTreeSet<_>>()
                            .len()
                            != request.codes.len() =>
                {
                    return Err(invalid_input("invalid billing redeem mint step"));
                }
                TransactionIrStep::BillingReserveStale {
                    cutoff_ts, limit, ..
                } if *cutoff_ts > i64::MAX as u64 || !(1..=10_000).contains(limit) => {
                    return Err(invalid_input("invalid billing stale-reserve step"));
                }
                TransactionIrStep::BillingWalletApply { request, .. }
                    if !valid_billing_text(&request.user_id, 200, true)
                        || !valid_billing_kind(&request.kind)
                        || !valid_billing_text(&request.ref_type, 100, false)
                        || !valid_billing_text(&request.ref_id, 300, false)
                        || !valid_billing_text(
                            &request.note,
                            crate::generated_tofudb_ir::MAX_BILLING_NOTE_CHARACTERS,
                            false,
                        )
                        || !valid_billing_text(&request.ledger_id, 200, true) =>
                {
                    return Err(invalid_input("invalid billing wallet apply step"));
                }
                TransactionIrStep::BillingWalletSettle { request, .. }
                    if !valid_billing_text(&request.user_id, 200, true)
                        || !valid_billing_text(&request.ref_id, 300, true)
                        || !valid_billing_text(
                            &request.note,
                            crate::generated_tofudb_ir::MAX_BILLING_NOTE_CHARACTERS,
                            false,
                        )
                        || !valid_billing_text(&request.release_id, 200, true)
                        || !valid_billing_text(&request.debit_id, 200, true) =>
                {
                    return Err(invalid_input("invalid billing wallet settle step"));
                }
                TransactionIrStep::TenantUserCreate { request, .. }
                    if request.user_id.is_empty()
                        || request.user_id.chars().count() > 256
                        || request.email.chars().count() > 320
                        || request.email != request.email.trim().to_lowercase()
                        || request.password_hash.chars().count() > 512
                        || request.display_name.chars().count() > 256
                        || !matches!(request.role.as_str(), "user" | "admin")
                        || !request.metadata.is_object()
                        || request.physical_updated_at_ms == 0 =>
                {
                    return Err(invalid_input("invalid tenant user create step"));
                }
                TransactionIrStep::TenantUserGet { selector, .. } => match selector {
                    crate::tenant_user::Selector::UserId(value)
                        if value.is_empty() || value.chars().count() > 256 =>
                    {
                        return Err(invalid_input("invalid tenant user selector"));
                    }
                    crate::tenant_user::Selector::Email(value)
                        if value.chars().count() > 320
                            || value.as_str() != value.trim().to_lowercase() =>
                    {
                        return Err(invalid_input("invalid tenant user selector"));
                    }
                    _ => {}
                },
                TransactionIrStep::TenantUserList { request, .. }
                    if !(1..=crate::generated_tofudb_ir::MAX_TENANT_USER_LIST_ROWS)
                        .contains(&request.limit)
                        || request
                            .offset
                            .checked_add(request.limit)
                            .is_none_or(|rows| {
                                rows > crate::generated_tofudb_ir::MAX_TENANT_USER_LIST_SCAN_ROWS
                            })
                        || request.status.as_deref().is_some_and(|status| {
                            !matches!(status, "active" | "suspended" | "deleted")
                        }) =>
                {
                    return Err(invalid_input("invalid tenant user list step"));
                }
                TransactionIrStep::TenantUserSetStatus {
                    user_id, status, ..
                } if user_id.is_empty()
                    || user_id.chars().count() > 256
                    || !matches!(status.as_str(), "active" | "suspended" | "deleted") =>
                {
                    return Err(invalid_input("invalid tenant user status step"));
                }
                TransactionIrStep::TenantUserSetRole { user_id, role, .. }
                    if user_id.is_empty()
                        || user_id.chars().count() > 256
                        || !matches!(role.as_str(), "user" | "admin") =>
                {
                    return Err(invalid_input("invalid tenant user role step"));
                }
                TransactionIrStep::TenantUserAuthentication { email, .. }
                    if email.chars().count() > 320
                        || email.as_str() != email.trim().to_lowercase() =>
                {
                    return Err(invalid_input("invalid tenant user authentication step"));
                }
                TransactionIrStep::TenantUserRecordLogin { user_id, .. }
                    if user_id.is_empty() || user_id.chars().count() > 256 =>
                {
                    return Err(invalid_input("invalid tenant user login step"));
                }
                TransactionIrStep::CredentialCreate { request, .. }
                | TransactionIrStep::CredentialCreateIfOwnerEmpty { request, .. }
                    if !valid_credential_boundary(&request.boundary)
                        || !valid_credential_id(&request.credential_id)
                        || request.account_user_id.chars().count() > 256
                        || request.name.chars().count() > 80
                        || request.prefix.is_empty()
                        || request.prefix.chars().count() > 32
                        || !valid_credential_secret(&request.secret_hash)
                        || request.scopes.len()
                            > crate::generated_tofudb_ir::MAX_CREDENTIAL_SCOPES
                        || request
                            .scopes
                            .iter()
                            .any(|scope| scope.is_empty() || scope.chars().count() > 128)
                        || request.scopes.windows(2).any(|pair| pair[0] >= pair[1])
                        || !valid_credential_timestamp(request.created_at)
                        || request
                            .expires_at
                            .is_some_and(|value| !valid_credential_timestamp(value))
                        || !request.metadata.is_object()
                        || request.physical_updated_at_ms == 0 =>
                {
                    return Err(invalid_input("invalid credential create step"));
                }
                TransactionIrStep::CredentialGet {
                    boundary,
                    credential_id,
                    ..
                }
                | TransactionIrStep::CredentialTouch {
                    boundary,
                    credential_id,
                    ..
                }
                | TransactionIrStep::CredentialUpdate {
                    boundary,
                    credential_id,
                    ..
                }
                | TransactionIrStep::CredentialRevoke {
                    boundary,
                    credential_id,
                    ..
                } if !valid_credential_boundary(boundary)
                    || !valid_credential_id(credential_id) =>
                {
                    return Err(invalid_input("invalid credential identity step"));
                }
                TransactionIrStep::CredentialList { boundary, .. }
                | TransactionIrStep::CredentialExists { boundary, .. }
                    if !valid_credential_boundary(boundary) =>
                {
                    return Err(invalid_input("invalid credential owner boundary"));
                }
                TransactionIrStep::CredentialAuthenticate {
                    secret_hash, now, ..
                }
                | TransactionIrStep::CredentialValidate {
                    secret_hash, now, ..
                } if !valid_credential_secret(secret_hash) || !valid_credential_timestamp(*now) => {
                    return Err(invalid_input("invalid credential validation step"));
                }
                TransactionIrStep::CredentialIdentify { secret_hash, .. }
                    if !valid_credential_secret(secret_hash) =>
                {
                    return Err(invalid_input("invalid credential identification step"));
                }
                TransactionIrStep::CredentialTouch {
                    used_at,
                    touch_if_before,
                    ..
                } if !valid_credential_timestamp(*used_at)
                    || !valid_credential_timestamp(*touch_if_before)
                    || touch_if_before > used_at =>
                {
                    return Err(invalid_input("invalid credential touch step"));
                }
                TransactionIrStep::CredentialUpdate { request, .. }
                    if request.physical_updated_at_ms == 0
                        || request
                            .name
                            .as_ref()
                            .is_some_and(|value| value.chars().count() > 80)
                        || request.scopes.as_ref().is_some_and(|values| {
                            values.len() > crate::generated_tofudb_ir::MAX_CREDENTIAL_SCOPES
                                || values
                                    .iter()
                                    .any(|value| value.is_empty() || value.chars().count() > 128)
                                || values.windows(2).any(|pair| pair[0] >= pair[1])
                        })
                        || request
                            .expires_at
                            .flatten()
                            .is_some_and(|value| !valid_credential_timestamp(value))
                        || request
                            .metadata
                            .as_ref()
                            .is_some_and(|value| !value.is_object())
                        || (request.name.is_none()
                            && request.scopes.is_none()
                            && request.rate_limit_rpm.is_none()
                            && request.rate_limit_tpd.is_none()
                            && request.expires_at.is_none()
                            && request.disabled.is_none()
                            && request.metadata.is_none()) =>
                {
                    return Err(invalid_input("invalid credential update step"));
                }
                TransactionIrStep::CredentialRevoke { revoked_at, .. }
                    if !valid_credential_timestamp(*revoked_at) =>
                {
                    return Err(invalid_input("invalid credential revoke step"));
                }
                _ => {}
            }
            let explicit_entity_keys: &[&EntityKey] = match step {
                TransactionIrStep::EntityRead { key, .. }
                | TransactionIrStep::EntityPut { key, .. }
                | TransactionIrStep::EntityDelete { key, .. }
                | TransactionIrStep::VersionedDocumentGet { key, .. }
                | TransactionIrStep::VersionedDocumentPut { key, .. }
                | TransactionIrStep::VersionedDocumentDelete { key, .. } => &[key],
                TransactionIrStep::VersionedDocumentList { start, end, .. } => &[start, end],
                _ => &[],
            };
            if explicit_entity_keys.iter().any(|key| {
                key.tenant_id() != self.tenant_id || key.owner_user_id() != self.owner_user_id
            }) {
                return Err(invalid_input(
                    "Transaction IR explicit entity key exceeds its owner scope",
                ));
            }
            match step {
                TransactionIrStep::BillingLedgerAppend { slot, .. }
                | TransactionIrStep::BillingLedgerFind { slot, .. }
                | TransactionIrStep::BillingLedgerList { slot, .. }
                | TransactionIrStep::BillingLedgerRecompute { slot, .. }
                | TransactionIrStep::BillingPaymentFind { slot, .. }
                | TransactionIrStep::BillingPaymentList { slot, .. }
                | TransactionIrStep::BillingPaymentRecord { slot, .. }
                | TransactionIrStep::BillingPaymentSettle { slot, .. }
                | TransactionIrStep::BillingRedeemCodeApply { slot, .. }
                | TransactionIrStep::BillingRedeemCodesList { slot, .. }
                | TransactionIrStep::BillingRedeemCodesMint { slot, .. }
                | TransactionIrStep::BillingReserveStale { slot, .. }
                | TransactionIrStep::BillingWalletApply { slot, .. }
                | TransactionIrStep::BillingWalletGet { slot, .. }
                | TransactionIrStep::BillingWalletSettle { slot, .. }
                | TransactionIrStep::ArtifactCreate { slot, .. }
                | TransactionIrStep::ArtifactGet { slot, .. }
                | TransactionIrStep::ArtifactList { slot, .. }
                | TransactionIrStep::ArtifactDelete { slot, .. }
                | TransactionIrStep::ArtifactVersions { slot, .. }
                | TransactionIrStep::ArtifactLibrary { slot, .. }
                | TransactionIrStep::ArtifactPin { slot, .. }
                | TransactionIrStep::ToolResultArtifactPut { slot, .. }
                | TransactionIrStep::ToolResultArtifactRead { slot, .. }
                | TransactionIrStep::ToolResultArtifactSearch { slot, .. }
                | TransactionIrStep::ToolResultArtifactPrune { slot, .. }
                | TransactionIrStep::SystemSchemaVersion { slot }
                | TransactionIrStep::RateLimitRecordAndCheck { slot, .. }
                | TransactionIrStep::ConversationCount { slot }
                | TransactionIrStep::CompactionArchiveList { slot, .. }
                | TransactionIrStep::CompactionArchiveGet { slot, .. }
                | TransactionIrStep::CompactionArchiveDeleteConversation { slot, .. }
                | TransactionIrStep::CompactionArchivePrune { slot, .. }
                | TransactionIrStep::EntityRead { slot, .. }
                | TransactionIrStep::VersionedDocumentGet { slot, .. }
                | TransactionIrStep::VersionedDocumentList { slot, .. }
                | TransactionIrStep::VersionedDocumentDelete { slot, .. }
                | TransactionIrStep::RecentProjectList { slot }
                | TransactionIrStep::RecentProjectClear { slot }
                | TransactionIrStep::DailyCostLatest { slot }
                | TransactionIrStep::IndexedStreamList { slot, .. }
                | TransactionIrStep::IndexedStreamInspectorSummary { slot, .. }
                | TransactionIrStep::IndexedStreamPrune { slot, .. }
                | TransactionIrStep::IndexedStreamBounds { slot, .. }
                | TransactionIrStep::IndexedStreamLatest { slot, .. }
                | TransactionIrStep::TenantUserCreate { slot, .. }
                | TransactionIrStep::TenantUserGet { slot, .. }
                | TransactionIrStep::TenantUserList { slot, .. }
                | TransactionIrStep::TenantUserSetStatus { slot, .. }
                | TransactionIrStep::TenantUserSetRole { slot, .. }
                | TransactionIrStep::TenantUserAuthentication { slot, .. }
                | TransactionIrStep::TenantUserRecordLogin { slot, .. }
                | TransactionIrStep::CredentialCreate { slot, .. }
                | TransactionIrStep::CredentialCreateIfOwnerEmpty { slot, .. }
                | TransactionIrStep::CredentialGet { slot, .. }
                | TransactionIrStep::CredentialList { slot, .. }
                | TransactionIrStep::CredentialExists { slot, .. }
                | TransactionIrStep::CredentialAuthenticate { slot, .. }
                | TransactionIrStep::CredentialValidate { slot, .. }
                | TransactionIrStep::CredentialIdentify { slot, .. }
                | TransactionIrStep::CredentialTouch { slot, .. }
                | TransactionIrStep::CredentialUpdate { slot, .. }
                | TransactionIrStep::CredentialRevoke { slot, .. } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                }
                TransactionIrStep::TaskResultCheckpoint {
                    task_id,
                    value_json,
                    updated_at_ms,
                    guarded,
                    require_parent,
                    cache_prefix_hwm,
                    last_turn_cache_read,
                    slot,
                    ..
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    task_result_bytes = task_result_bytes
                        .checked_add(value_json.len())
                        .filter(|bytes| *bytes <= MAX_TASK_RESULT_DOCUMENT_BYTES)
                        .ok_or_else(|| {
                            invalid_input("task result transaction exceeds its 64 MiB bound")
                        })?;
                    if task_id.is_empty()
                        || task_id.chars().count() > 512
                        || *updated_at_ms == 0
                        || value_json.len() > MAX_TASK_RESULT_DOCUMENT_BYTES
                        || (!*guarded
                            && (*require_parent
                                || cache_prefix_hwm.is_some()
                                || last_turn_cache_read.is_some()))
                        || (cache_prefix_hwm.is_some() || last_turn_cache_read.is_some())
                            && (!*guarded || !*require_parent)
                        || cache_prefix_hwm
                            .iter()
                            .chain(last_turn_cache_read.iter())
                            .any(|value| !(1..=MAX_TASK_RESULT_CACHE_FACT).contains(value))
                        || !serde_json::from_slice::<serde_json::Value>(value_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid task result checkpoint step"));
                    }
                }
                TransactionIrStep::TaskResultReplayGet {
                    task_id,
                    requested_user_id,
                    ..
                }
                | TransactionIrStep::TaskResultAbortRequested {
                    task_id,
                    requested_user_id,
                    ..
                } => {
                    let slot = match step {
                        TransactionIrStep::TaskResultReplayGet { slot, .. }
                        | TransactionIrStep::TaskResultAbortRequested { slot, .. } => *slot,
                        _ => unreachable!(),
                    };
                    validate_worker_job_slot(&mut initialized_slots, slot)?;
                    if task_id.is_empty()
                        || task_id.chars().count() > 512
                        || *requested_user_id == 0
                    {
                        return Err(invalid_input("invalid task result identity step"));
                    }
                }
                TransactionIrStep::TaskResultAbort {
                    task_id,
                    requested_user_id,
                    source,
                    requested_at_ms,
                    slot,
                    ..
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if task_id.is_empty()
                        || task_id.chars().count() > 512
                        || source.is_empty()
                        || source.chars().count() > 128
                        || *requested_at_ms == 0
                        || *requested_user_id == 0
                    {
                        return Err(invalid_input("invalid task result abort step"));
                    }
                }
                TransactionIrStep::TaskResultSummaryList {
                    status,
                    requested_user_id,
                    conversation_id,
                    completed_before_ms,
                    limit,
                    scan_limit,
                    order_by,
                    after_key,
                    slot,
                    ..
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if requested_user_id.is_some_and(|value| value == 0)
                        || status
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 64)
                        || conversation_id
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 512)
                        || completed_before_ms.is_some_and(|value| value < 0)
                        || !(1..=MAX_TASK_RESULT_SUMMARY_ROWS).contains(limit)
                        || !(1..=MAX_TASK_RESULT_SUMMARY_SCAN_ROWS).contains(scan_limit)
                        || !matches!(
                            order_by.as_str(),
                            "created_at_desc" | "completed_at_asc" | "updated_at_asc"
                        )
                        || after_key.chars().count() > 512
                    {
                        return Err(invalid_input("invalid task result summary step"));
                    }
                }
                TransactionIrStep::TaskResultRecoverRunning {
                    interrupted_reason,
                    maximum_rows,
                    scan_limit,
                    updated_at_ms,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !matches!(
                        interrupted_reason.as_str(),
                        "server_restart" | "process_killed" | "manual_restart"
                    ) || !(1..=MAX_TASK_RESULT_RECOVERY_ROWS).contains(maximum_rows)
                        || !(1..=MAX_TASK_RESULT_RECOVERY_SCAN_ROWS).contains(scan_limit)
                        || *updated_at_ms == 0
                    {
                        return Err(invalid_input("invalid task result recovery step"));
                    }
                }
                TransactionIrStep::TaskResultCostExperimentScan {
                    requested_user_id,
                    experiment_id,
                    completed_at_gte,
                    limit,
                    scan_limit,
                    after_key,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if *requested_user_id == 0
                        || experiment_id.is_empty()
                        || experiment_id.chars().count() > 128
                        || *completed_at_gte < 0
                        || !(1..=MAX_TASK_RESULT_COST_EXPERIMENT_ROWS).contains(limit)
                        || !(1..=MAX_TASK_RESULT_COST_EXPERIMENT_SCAN_ROWS).contains(scan_limit)
                        || after_key.chars().count() > 1024
                    {
                        return Err(invalid_input(
                            "invalid task result cost-experiment scan step",
                        ));
                    }
                }
                TransactionIrStep::CompactionArchiveCreate {
                    archive_id,
                    conversation_id,
                    messages_json,
                    summary,
                    receipt_json,
                    trigger,
                    task_id,
                    model,
                    reason,
                    committed_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if archive_id.is_empty()
                        || archive_id.chars().count() > 128
                        || conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || summary.chars().count() > 200_000
                        || receipt_json.len() > 32 * 1024
                        || trigger.chars().count() > 32
                        || task_id.chars().count() > 512
                        || model.chars().count() > 256
                        || reason.chars().count() > 500
                        || *committed_at_ms == 0
                        || !serde_json::from_slice::<serde_json::Value>(messages_json).is_ok_and(
                            |value| {
                                value.as_array().is_some_and(|items| {
                                    items.iter().all(|item| {
                                        item.as_object().is_some_and(|message| {
                                            !message.contains_key("_tofuArchivedMessageCodec")
                                                && !message
                                                    .contains_key("_tofuStorageProjectionCodec")
                                        })
                                    })
                                })
                            },
                        )
                        || !serde_json::from_slice::<serde_json::Value>(receipt_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid compaction archive create step"));
                    }
                    literal_bytes = [
                        archive_id.len(),
                        conversation_id.len(),
                        messages_json.len(),
                        summary.len(),
                        receipt_json.len(),
                        trigger.len(),
                        task_id.len(),
                        model.len(),
                        reason.len(),
                    ]
                    .into_iter()
                    .try_fold(literal_bytes, |bytes, value| bytes.checked_add(value))
                    .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                    .ok_or_else(|| invalid_input("Transaction IR literal bytes exceed 8 MiB"))?;
                }
                TransactionIrStep::CompactionArchiveUpdateSummary {
                    archive_id,
                    summary,
                    receipt_json,
                    committed_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if archive_id.is_empty()
                        || archive_id.chars().count() > 128
                        || summary.chars().count() > 200_000
                        || *committed_at_ms == 0
                        || receipt_json.as_ref().is_some_and(|value| {
                            value.len() > 32 * 1024
                                || !serde_json::from_slice::<serde_json::Value>(value)
                                    .is_ok_and(|value| value.is_object())
                        })
                    {
                        return Err(invalid_input("invalid compaction archive summary step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(archive_id.len())
                        .and_then(|bytes| bytes.checked_add(summary.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(receipt_json.as_ref().map_or(0, Vec::len))
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::VersionedDocumentPut {
                    namespace,
                    logical_key,
                    value_json,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(namespace.len())
                        .and_then(|bytes| bytes.checked_add(logical_key.len()))
                        .and_then(|bytes| bytes.checked_add(value_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                    if *updated_at_ms == 0 {
                        return Err(invalid_input("Transaction IR document timestamp is zero"));
                    }
                }
                TransactionIrStep::RecentProjectTouch {
                    path,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if path.is_empty()
                        || path.chars().count() > MAX_RECENT_PROJECT_PATH_CHARACTERS
                        || *updated_at_ms == 0
                    {
                        return Err(invalid_input("invalid recent-project touch step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(path.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProjectRelink {
                    old_path,
                    new_path,
                    updated_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if old_path.is_empty()
                        || old_path.chars().count() > MAX_RECENT_PROJECT_PATH_CHARACTERS
                        || new_path.is_empty()
                        || new_path.chars().count() > MAX_RECENT_PROJECT_PATH_CHARACTERS
                        || old_path == new_path
                        || *updated_at_ms == 0
                    {
                        return Err(invalid_input("invalid project relink step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(old_path.len())
                        .and_then(|bytes| bytes.checked_add(new_path.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Queue { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Orchestration { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::IntegrationWorkspace { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Swarm { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Scheduler { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate(self.owner_user_id)?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Timer { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate(self.owner_user_id)?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::DailyCostUpsert {
                    date,
                    cost,
                    conversations_json,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !crate::daily_cost::valid_date(date)
                        || !cost.is_finite()
                        || !(0.0..=1_000_000_000.0).contains(cost)
                        || *updated_at_ms == 0
                        || conversations_json.len()
                            > crate::generated_tofudb_ir::MAX_DAILY_COST_DOCUMENT_BYTES
                        || !serde_json::from_slice::<serde_json::Value>(conversations_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid daily-cost upsert step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(date.len())
                        .and_then(|bytes| bytes.checked_add(conversations_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::DailyCostMonth { year, month, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !(1970..=9999).contains(year) || !(1..=12).contains(month) {
                        return Err(invalid_input("invalid daily-cost month step"));
                    }
                }
                TransactionIrStep::DailyCostPersistedDates { dates, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if dates.len() > crate::generated_tofudb_ir::MAX_DAILY_COST_PERSISTED_DATES
                        || dates
                            .iter()
                            .any(|date| !crate::daily_cost::valid_date(date))
                        || dates
                            .iter()
                            .collect::<std::collections::BTreeSet<_>>()
                            .len()
                            != dates.len()
                    {
                        return Err(invalid_input("invalid daily-cost date-probe step"));
                    }
                    literal_bytes = dates
                        .iter()
                        .try_fold(literal_bytes, |bytes, date| bytes.checked_add(date.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::DailyCostDelete { date, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if date
                        .as_deref()
                        .is_some_and(|date| !crate::daily_cost::valid_date(date))
                    {
                        return Err(invalid_input("invalid daily-cost delete step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(date.as_ref().map_or(0, String::len))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::LogAggregateFlush {
                    rows_json,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if *updated_at_ms == 0
                        || !serde_json::from_slice::<Vec<crate::log_aggregate::FlushRow>>(rows_json)
                            .is_ok_and(|rows| {
                                rows.len()
                                    <= crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_FLUSH_BATCH
                                    && rows.iter().all(|row| row.valid())
                            })
                    {
                        return Err(invalid_input("invalid log-aggregate flush step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(rows_json.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::LogAggregateQuery {
                    level,
                    q,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if level.chars().count()
                        > crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_LEVEL_CHARACTERS
                        || q.chars().count()
                            > crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS
                        || !(1..=crate::generated_tofudb_ir::MAX_LOG_AGGREGATE_QUERY_ROWS)
                            .contains(limit)
                    {
                        return Err(invalid_input("invalid log-aggregate query step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(level.len())
                        .and_then(|bytes| bytes.checked_add(q.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::PluginRegister {
                    manifest_json,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if serde_json::from_slice::<Value>(manifest_json).is_err() {
                        return Err(invalid_input("invalid plugin manifest step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(manifest_json.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::PluginManifestGet { namespace, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if namespace.chars().count()
                        > crate::generated_tofudb_ir::MAX_PLUGIN_NAMESPACE_CHARACTERS
                    {
                        return Err(invalid_input("invalid plugin manifest step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(namespace.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Paper { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::PaperLibrary { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::PaperArtifact { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::PaperPodcast { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::RawArchive { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Compressed bodies are bounded blob inputs. Count only
                    // routing metadata against the inline IR literal budget.
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Research { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Report/workspace bodies are separately bounded blob
                    // inputs rather than inline Transaction IR literals.
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Optimizer { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate(self.owner_user_id)?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::Knowledge { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Document/chunk bodies are separately bounded blob
                    // inputs rather than inline Transaction IR literals.
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProjectBrainCommand {
                    project_key,
                    payload_json,
                    timestamp,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if crate::project_brain::normalize_project_key(project_key).as_deref()
                        != Some(project_key.as_str())
                        || *timestamp > i64::MAX as u64
                        || payload_json.len()
                            > crate::generated_tofudb_ir::MAX_PROJECT_BRAIN_DOCUMENT_BYTES
                        || !serde_json::from_slice::<serde_json::Value>(payload_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid Project Brain command step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(project_key.len())
                        .and_then(|bytes| bytes.checked_add(payload_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProjectBrainActiveList { slot }
                | TransactionIrStep::ProjectBrainRecoverySnapshot { slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                }
                TransactionIrStep::ProjectBrainRebuild {
                    project_key,
                    timestamp,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if crate::project_brain::normalize_project_key(project_key).as_deref()
                        != Some(project_key.as_str())
                        || *timestamp > i64::MAX as u64
                    {
                        return Err(invalid_input("invalid Project Brain rebuild step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(project_key.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProjectBrainGet { project_key, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if crate::project_brain::normalize_project_key(project_key).as_deref()
                        != Some(project_key.as_str())
                    {
                        return Err(invalid_input("invalid Project Brain get step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(project_key.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::RecentProjectTouchMany {
                    paths,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if paths.is_empty()
                        || paths.len() > MAX_RECENT_PROJECT_TOUCH_BATCH
                        || paths.iter().any(|path| {
                            path.is_empty()
                                || path.chars().count() > MAX_RECENT_PROJECT_PATH_CHARACTERS
                        })
                        || paths
                            .iter()
                            .collect::<std::collections::BTreeSet<_>>()
                            .len()
                            != paths.len()
                        || *updated_at_ms == 0
                    {
                        return Err(invalid_input("invalid recent-project batch touch step"));
                    }
                    literal_bytes = paths
                        .iter()
                        .try_fold(literal_bytes, |bytes, path| bytes.checked_add(path.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::BrowserSiteObservationRecord {
                    origin,
                    route_family,
                    operation,
                    outcome,
                    observed_at_ms,
                    observation_json,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !crate::browser_site_observation::valid_identity(
                        origin,
                        route_family,
                        operation,
                    ) || !matches!(
                        outcome.as_str(),
                        "success"
                            | "not_observed"
                            | "structure_mismatch"
                            | "not_found"
                            | "auth_challenge"
                            | "rate_limited"
                            | "transient_failure"
                            | "policy_denied"
                    ) || *observed_at_ms == 0
                        || *committed_at_ms == 0
                        || (outcome == "success") != observation_json.is_some()
                        || observation_json.as_ref().is_some_and(|bytes| {
                            bytes.len()
                                > crate::generated_tofudb_ir::MAX_BROWSER_SITE_OBSERVATION_BYTES
                        })
                    {
                        return Err(invalid_input(
                            "invalid browser site observation record step",
                        ));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(origin.len())
                        .and_then(|bytes| bytes.checked_add(route_family.len()))
                        .and_then(|bytes| bytes.checked_add(operation.len()))
                        .and_then(|bytes| bytes.checked_add(outcome.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(observation_json.as_ref().map_or(0, Vec::len))
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::BrowserSiteObservationGet {
                    origin,
                    route_family,
                    operation,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !crate::browser_site_observation::valid_identity(
                        origin,
                        route_family,
                        operation,
                    ) {
                        return Err(invalid_input("invalid browser site observation get step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(origin.len())
                        .and_then(|bytes| bytes.checked_add(route_family.len()))
                        .and_then(|bytes| bytes.checked_add(operation.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationCreate {
                    conversation_id,
                    title,
                    settings_json,
                    committed_at_ms,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || title.chars().count() > 500
                        || *committed_at_ms == 0
                        || !serde_json::from_slice::<serde_json::Value>(settings_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid conversation create step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(title.len()))
                        .and_then(|bytes| bytes.checked_add(settings_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationDelete {
                    conversation_id,
                    deleted_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || *deleted_at_ms == 0
                    {
                        return Err(invalid_input("invalid conversation delete step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationRestore {
                    conversation_id,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid conversation restore step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationPurge {
                    conversation_id,
                    purged_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || *purged_at_ms == 0
                    {
                        return Err(invalid_input("invalid conversation purge step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationTrashPrune {
                    deleted_before_ms,
                    maximum_conversations,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if *deleted_before_ms == 0
                        || *maximum_conversations == 0
                        || *maximum_conversations > 64
                    {
                        return Err(invalid_input("invalid conversation trash prune step"));
                    }
                }
                TransactionIrStep::ConversationGet {
                    conversation_id,
                    include_messages,
                    message_window,
                    before_sequence,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || *message_window > 500
                        || before_sequence.is_some() && *message_window == 0
                        || !include_messages && (*message_window != 0 || before_sequence.is_some())
                    {
                        return Err(invalid_input("invalid conversation get step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationSettingsUpdate {
                    conversation_id,
                    updates_json,
                    replace,
                    expected_settings_json,
                    committed_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    let updates_are_object =
                        serde_json::from_slice::<serde_json::Value>(updates_json)
                            .is_ok_and(|value| value.is_object());
                    let expected_is_object = expected_settings_json.as_ref().is_some_and(|value| {
                        serde_json::from_slice::<serde_json::Value>(value)
                            .is_ok_and(|value| value.is_object())
                    });
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || !updates_are_object
                        || (*replace && !expected_is_object)
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid conversation settings update step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(updates_json.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(expected_settings_json.as_ref().map_or(0, Vec::len))
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationMetadataUpdate {
                    conversation_id,
                    title,
                    updated_at_ms,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || title.is_none() && updated_at_ms.is_none()
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid conversation metadata update step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(title.as_ref().map_or(0, String::len)))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::EntityPut {
                    value, condition, ..
                } => {
                    literal_bytes = literal_bytes
                        .checked_add(value.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                    if let Some(slot) = referenced_slot(*condition) {
                        if !initialized_slots
                            .get(slot as usize)
                            .copied()
                            .unwrap_or(false)
                        {
                            return Err(invalid_input(
                                "Transaction IR condition references an unread slot",
                            ));
                        }
                    }
                }
                TransactionIrStep::EntityDelete { condition, .. } => {
                    if let Some(slot) = referenced_slot(*condition) {
                        if !initialized_slots
                            .get(slot as usize)
                            .copied()
                            .unwrap_or(false)
                        {
                            return Err(invalid_input(
                                "Transaction IR condition references an unread slot",
                            ));
                        }
                    }
                }
                TransactionIrStep::StreamAppend {
                    expected_next_sequence,
                    events,
                    ..
                } => {
                    if *expected_next_sequence == 0 || events.is_empty() {
                        return Err(invalid_input("Transaction IR stream append is empty"));
                    }
                    literal_bytes = events.iter().try_fold(literal_bytes, |bytes, event| {
                        bytes
                            .checked_add(event.encoded_len())
                            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal bytes exceed 8 MiB")
                            })
                    })?;
                }
                TransactionIrStep::ConversationClone {
                    source_conversation_id,
                    destination_conversation_id,
                    title,
                    committed_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if source_conversation_id.is_empty()
                        || source_conversation_id.chars().count() > 256
                        || destination_conversation_id.is_empty()
                        || destination_conversation_id.chars().count() > 256
                        || source_conversation_id == destination_conversation_id
                        || title.as_ref().is_some_and(|value| {
                            value.trim().is_empty() || value.chars().count() > 500
                        })
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid conversation clone step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(source_conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(destination_conversation_id.len()))
                        .and_then(|bytes| bytes.checked_add(title.as_ref().map_or(0, String::len)))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::IndexedStreamAppend {
                    task_id,
                    payload_json,
                    created_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if task_id.is_empty() || *created_at_ms <= 0 {
                        return Err(invalid_input("invalid indexed stream append"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(payload_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::IndexedStreamAppendBatch { items, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if items.is_empty() || items.len() > 500 {
                        return Err(invalid_input("Transaction IR event batch is invalid"));
                    }
                    for item in items {
                        if item.task_id.is_empty() || item.created_at_ms <= 0 {
                            return Err(invalid_input("invalid indexed stream batch item"));
                        }
                        literal_bytes = literal_bytes
                            .checked_add(item.task_id.len())
                            .and_then(|bytes| bytes.checked_add(item.payload_json.len()))
                            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal bytes exceed 8 MiB")
                            })?;
                    }
                }
                TransactionIrStep::ConversationCatalogPage {
                    folder_id,
                    before_updated_at_ms,
                    before_id,
                    limit,
                    settings_keys,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if *limit == 0
                        || *limit > 1_000
                        || folder_id
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 512)
                        || before_id.chars().count() > 256
                        || before_updated_at_ms.is_none() && !before_id.is_empty()
                        || settings_keys.as_ref().is_some_and(|keys| {
                            keys.len() > 64 || keys.iter().any(String::is_empty)
                        })
                    {
                        return Err(invalid_input("invalid conversation catalog page step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(folder_id.as_ref().map_or(0, String::len))
                        .and_then(|bytes| bytes.checked_add(before_id.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(
                                settings_keys
                                    .as_ref()
                                    .map_or(0, |keys| keys.iter().map(String::len).sum()),
                            )
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationList {
                    project_path,
                    title_contains,
                    ids,
                    settings_keys,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if *limit > 10_000
                        || project_path
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 4_096)
                        || title_contains
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 512)
                        || ids
                            .as_ref()
                            .is_some_and(|ids| ids.iter().any(String::is_empty))
                        || settings_keys.as_ref().is_some_and(|keys| {
                            keys.len() > 64 || keys.iter().any(String::is_empty)
                        })
                    {
                        return Err(invalid_input("invalid conversation list step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(project_path.as_ref().map_or(0, String::len))
                        .and_then(|bytes| {
                            bytes.checked_add(title_contains.as_ref().map_or(0, String::len))
                        })
                        .and_then(|bytes| {
                            bytes.checked_add(
                                ids.as_ref()
                                    .map_or(0, |ids| ids.iter().map(String::len).sum()),
                            )
                        })
                        .and_then(|bytes| {
                            bytes.checked_add(
                                settings_keys
                                    .as_ref()
                                    .map_or(0, |keys| keys.iter().map(String::len).sum()),
                            )
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ConversationActivityDates {
                    day_boundaries_ms,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !(2..=367).contains(&day_boundaries_ms.len())
                        || day_boundaries_ms.windows(2).any(|pair| pair[0] >= pair[1])
                        || !(1..=10_000).contains(limit)
                    {
                        return Err(invalid_input("invalid conversation activity-dates step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(day_boundaries_ms.len() * std::mem::size_of::<i64>())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ModelRoutingGet { tenant_label, slot }
                | TransactionIrStep::ModelRoutingMigrationReceiptGet { tenant_label, slot }
                | TransactionIrStep::ModelRoutingSecretList { tenant_label, slot } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ModelRoutingCommit {
                    tenant_label,
                    document_json,
                    migration_receipt_json,
                    updated_at,
                    slot,
                    ..
                } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    if !valid_model_routing_json_object(document_json)
                        || migration_receipt_json
                            .as_ref()
                            .is_some_and(|value| !valid_model_routing_json_object(value))
                        || !updated_at.is_finite()
                        || !(0.0..=MAX_MODEL_ROUTING_TIMESTAMP_SECONDS).contains(updated_at)
                    {
                        return Err(invalid_input("invalid model-routing commit step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(document_json.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(migration_receipt_json.as_ref().map_or(0, Vec::len))
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ModelRoutingMigrationReceiptPut {
                    tenant_label,
                    receipt_json,
                    initial_document_json,
                    updated_at,
                    slot,
                } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    if !valid_model_routing_json_object(receipt_json)
                        || initial_document_json
                            .as_ref()
                            .is_some_and(|value| !valid_model_routing_json_object(value))
                        || !updated_at.is_finite()
                        || !(0.0..=MAX_MODEL_ROUTING_TIMESTAMP_SECONDS).contains(updated_at)
                    {
                        return Err(invalid_input("invalid model-routing receipt step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(receipt_json.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(initial_document_json.as_ref().map_or(0, Vec::len))
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ModelRoutingSecretGet {
                    tenant_label,
                    secret_reference,
                    slot,
                }
                | TransactionIrStep::ModelRoutingSecretDelete {
                    tenant_label,
                    secret_reference,
                    slot,
                } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    if secret_reference.is_empty()
                        || secret_reference.chars().count()
                            > MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS
                    {
                        return Err(invalid_input("invalid model-routing secret identity step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(secret_reference.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ModelRoutingSecretPut {
                    tenant_label,
                    secret_reference,
                    ciphertext,
                    key_hint,
                    updated_at,
                    slot,
                } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    if secret_reference.is_empty()
                        || secret_reference.chars().count()
                            > MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS
                        || ciphertext.is_empty()
                        || ciphertext.chars().count() > MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS
                        || key_hint.chars().count() > MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS
                        || !updated_at.is_finite()
                        || !(0.0..=MAX_MODEL_ROUTING_TIMESTAMP_SECONDS).contains(updated_at)
                    {
                        return Err(invalid_input("invalid model-routing secret put step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(secret_reference.len()))
                        .and_then(|bytes| bytes.checked_add(ciphertext.len()))
                        .and_then(|bytes| bytes.checked_add(key_hint.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ModelRoutingSecretPrune {
                    tenant_label,
                    active_secret_references,
                    updated_before,
                    slot,
                } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    if active_secret_references.len() > MAX_MODEL_ROUTING_SECRETS_PER_OWNER_BOUNDARY
                        || active_secret_references.iter().any(|reference| {
                            reference.is_empty()
                                || reference.chars().count()
                                    > MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS
                        })
                        || !updated_before.is_finite()
                        || !(0.0..=MAX_MODEL_ROUTING_TIMESTAMP_SECONDS).contains(updated_before)
                    {
                        return Err(invalid_input("invalid model-routing secret prune step"));
                    }
                    literal_bytes = active_secret_references
                        .iter()
                        .try_fold(literal_bytes + tenant_label.len(), |bytes, reference| {
                            bytes.checked_add(reference.len())
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProviderCreate {
                    tenant_label,
                    provider_id,
                    document_json,
                    physical_updated_at_ms,
                    slot,
                } => {
                    validate_provider_step_slot(
                        &mut initialized_slots,
                        *slot,
                        tenant_label,
                        Some(provider_id),
                    )?;
                    if document_json.is_empty() || *physical_updated_at_ms == 0 {
                        return Err(invalid_input("invalid provider create step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(provider_id.len()))
                        .and_then(|bytes| bytes.checked_add(document_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProviderGet {
                    tenant_label,
                    provider_id,
                    slot,
                }
                | TransactionIrStep::ProviderDelete {
                    tenant_label,
                    provider_id,
                    slot,
                } => {
                    validate_provider_step_slot(
                        &mut initialized_slots,
                        *slot,
                        tenant_label,
                        Some(provider_id),
                    )?;
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(provider_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProviderList { tenant_label, slot } => {
                    validate_provider_step_slot(&mut initialized_slots, *slot, tenant_label, None)?;
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProviderUpdate {
                    tenant_label,
                    provider_id,
                    updates_json,
                    updated_at,
                    physical_updated_at_ms,
                    slot,
                } => {
                    validate_provider_step_slot(
                        &mut initialized_slots,
                        *slot,
                        tenant_label,
                        Some(provider_id),
                    )?;
                    if updates_json.is_empty()
                        || !updated_at.is_finite()
                        || *updated_at < 0.0
                        || *physical_updated_at_ms == 0
                    {
                        return Err(invalid_input("invalid provider update step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(provider_id.len()))
                        .and_then(|bytes| bytes.checked_add(updates_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::ProviderTouch {
                    tenant_label,
                    provider_id,
                    used_at,
                    physical_updated_at_ms,
                    slot,
                } => {
                    validate_provider_step_slot(
                        &mut initialized_slots,
                        *slot,
                        tenant_label,
                        Some(provider_id),
                    )?;
                    if !used_at.is_finite() || *used_at < 0.0 || *physical_updated_at_ms == 0 {
                        return Err(invalid_input("invalid provider touch step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .and_then(|bytes| bytes.checked_add(provider_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobEnqueue {
                    task_id,
                    user_id,
                    tenant_label,
                    task_kind,
                    payload_json,
                    idempotency_key,
                    request_digest,
                    priority,
                    available_at_ms,
                    now_ms,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id, 256, false)
                        || *user_id == 0
                        || !valid_worker_job_text(tenant_label, 256, true)
                        || !valid_worker_job_text(task_kind, 128, false)
                        || !valid_worker_job_text(idempotency_key, 256, false)
                        || request_digest.len() != 64
                        || !request_digest.bytes().all(|byte| byte.is_ascii_hexdigit())
                        || usize::from(*priority) > MAX_WORKER_JOB_PRIORITY
                        || *available_at_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || *now_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || payload_json.len() > MAX_WORKER_JOB_PAYLOAD_BYTES
                        || serde_json::from_slice::<serde_json::Value>(payload_json)
                            .ok()
                            .and_then(|value| value.as_object().cloned())
                            .is_none()
                    {
                        return Err(invalid_input("invalid worker job enqueue step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(tenant_label.len()))
                        .and_then(|bytes| bytes.checked_add(task_kind.len()))
                        .and_then(|bytes| bytes.checked_add(payload_json.len()))
                        .and_then(|bytes| bytes.checked_add(idempotency_key.len()))
                        .and_then(|bytes| bytes.checked_add(request_digest.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobGet {
                    task_id,
                    user_id,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id, 256, false) || *user_id == 0 {
                        return Err(invalid_input("invalid worker job get step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobClaimNext {
                    worker_id,
                    now_ms,
                    lease_ms,
                    task_kinds,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    let unique: std::collections::BTreeSet<_> = task_kinds.iter().collect();
                    if !valid_worker_job_text(worker_id, 256, false)
                        || *now_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || !(10_000..=300_000).contains(lease_ms)
                        || task_kinds.is_empty()
                        || task_kinds.len() > MAX_WORKER_JOB_CLAIM_KINDS
                        || unique.len() != task_kinds.len()
                        || task_kinds
                            .iter()
                            .any(|kind| !valid_worker_job_text(kind, 128, false))
                    {
                        return Err(invalid_input("invalid worker job claim step"));
                    }
                    literal_bytes = task_kinds
                        .iter()
                        .try_fold(literal_bytes + worker_id.len(), |bytes, kind| {
                            bytes.checked_add(kind.len())
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobHeartbeat {
                    task_id,
                    worker_id,
                    fencing_token,
                    now_ms,
                    lease_ms,
                    slot,
                    ..
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id, 256, false)
                        || !valid_worker_job_text(worker_id, 256, false)
                        || *fencing_token == 0
                        || *now_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || !(10_000..=300_000).contains(lease_ms)
                    {
                        return Err(invalid_input("invalid worker job heartbeat step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(worker_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobClaimState {
                    task_id,
                    worker_id,
                    fencing_token,
                    now_ms,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id, 256, false)
                        || !valid_worker_job_text(worker_id, 256, false)
                        || *fencing_token == 0
                        || *now_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                    {
                        return Err(invalid_input("invalid worker job claim-state step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(worker_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobRequestCancel {
                    task_id,
                    user_id,
                    now_ms,
                    reason,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id, 256, false)
                        || *user_id == 0
                        || *now_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || !valid_worker_job_text(reason, 1000, true)
                    {
                        return Err(invalid_input("invalid worker job cancellation step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(reason.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::WorkerJobComplete {
                    task_id,
                    worker_id,
                    fencing_token,
                    now_ms,
                    terminal_status,
                    result_ref,
                    error_json,
                    slot,
                    ..
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id, 256, false)
                        || !valid_worker_job_text(worker_id, 256, false)
                        || *fencing_token == 0
                        || *now_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || !matches!(
                            terminal_status.as_str(),
                            "succeeded" | "failed" | "cancelled"
                        )
                        || !valid_worker_job_text(result_ref, 1024, true)
                        || error_json.len() > MAX_WORKER_JOB_ERROR_BYTES
                        || serde_json::from_slice::<serde_json::Value>(error_json)
                            .ok()
                            .and_then(|value| value.as_object().cloned())
                            .is_none()
                    {
                        return Err(invalid_input("invalid worker job completion step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(worker_id.len()))
                        .and_then(|bytes| bytes.checked_add(terminal_status.len()))
                        .and_then(|bytes| bytes.checked_add(result_ref.len()))
                        .and_then(|bytes| bytes.checked_add(error_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnCreatePair { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    let encoded = serde_json::to_vec(&json!({
                        "conversationId": request.conversation_id,
                        "commandId": request.command_id,
                        "projection": request.input_projection,
                        "config": request.config,
                    }))
                    .map_err(|_| invalid_input("invalid Turn pair step"))?;
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.command_id.is_empty()
                        || request.command_id.chars().count() > 256
                        || request.lane_id.is_empty()
                        || request.lane_id.chars().count() > 256
                        || request.input_turn_id.is_empty()
                        || request.output_turn_id.is_empty()
                        || request.output_attempt_id.is_empty()
                        || request.committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn pair step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(encoded.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnQueueActivate { request, slot }
                | TransactionIrStep::TurnQueueCancel { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.queue_id.is_empty()
                        || request.queue_id.chars().count() > 256
                        || request.committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn queue transition step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(request.conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(request.queue_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnSteerCommit { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.command_id.is_empty()
                        || request.command_id.chars().count() > 256
                        || request.text.is_empty()
                        || request.text.chars().count() > 1024 * 1024
                        || request.updated_at_ms == 0
                        || request.committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn steer commit step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(request.conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(request.attempt_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.command_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.text.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnRelatedAnnounce { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.turn_ids.len() > crate::turn::MAX_RELATED_TURNS_PER_ANNOUNCEMENT
                        || request
                            .turn_ids
                            .iter()
                            .any(|turn_id| turn_id.is_empty() || turn_id.chars().count() > 128)
                        || request.updated_at_ms == 0
                        || request.committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn related announce step"));
                    }
                    literal_bytes = request.turn_ids.iter().try_fold(
                        literal_bytes
                            .checked_add(request.attempt_id.len())
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?,
                        |bytes, turn_id| {
                            bytes.checked_add(turn_id.len()).ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })
                        },
                    )?;
                    if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
                        return Err(invalid_input("Transaction IR literal bytes exceed 8 MiB"));
                    }
                }
                TransactionIrStep::TurnVisibleSync { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.root_turn_id.is_empty()
                        || request.root_turn_id.chars().count() > 128
                        || request.default_kind.chars().count() > 128
                        || request.run_id.chars().count() > 256
                        || request.messages.len()
                            > crate::turn::MAX_VISIBLE_TURNS_PER_SYNCHRONIZATION
                        || request.updated_at_ms == 0
                        || request.committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid visible Turn synchronization step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(request.conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(request.attempt_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.root_turn_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.default_kind.len()))
                        .and_then(|bytes| bytes.checked_add(request.run_id.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                    for message in &request.messages {
                        literal_bytes = literal_bytes
                            .checked_add(
                                serde_json::to_vec(message)
                                    .map_err(|_| {
                                        invalid_input("visible Turn message cannot be encoded")
                                    })?
                                    .len(),
                            )
                            .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal bytes exceed 8 MiB")
                            })?;
                    }
                }
                TransactionIrStep::TurnAppendSettled {
                    conversation_id,
                    actor,
                    status,
                    projection_json,
                    settlement_json,
                    lane_id,
                    command_id,
                    kind,
                    run_id,
                    turn_id,
                    attempt_id,
                    created_at_ms: _,
                    committed_at_ms,
                    default_title,
                    default_settings_json,
                    default_created_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || actor.is_empty()
                        || status.is_empty()
                        || lane_id.is_empty()
                        || lane_id.chars().count() > 256
                        || command_id.is_empty()
                        || command_id.chars().count() > 256
                        || kind.chars().count() > 128
                        || run_id.chars().count() > 256
                        || turn_id.is_empty()
                        || turn_id.chars().count() > 128
                        || attempt_id
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 128)
                        || *committed_at_ms == 0
                        || *default_created_at_ms == 0
                    {
                        return Err(invalid_input("invalid settled Turn append step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(actor.len()))
                        .and_then(|bytes| bytes.checked_add(status.len()))
                        .and_then(|bytes| bytes.checked_add(projection_json.len()))
                        .and_then(|bytes| bytes.checked_add(settlement_json.len()))
                        .and_then(|bytes| bytes.checked_add(lane_id.len()))
                        .and_then(|bytes| bytes.checked_add(command_id.len()))
                        .and_then(|bytes| bytes.checked_add(kind.len()))
                        .and_then(|bytes| bytes.checked_add(run_id.len()))
                        .and_then(|bytes| bytes.checked_add(turn_id.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(attempt_id.as_ref().map_or(0, String::len))
                        })
                        .and_then(|bytes| bytes.checked_add(default_title.len()))
                        .and_then(|bytes| bytes.checked_add(default_settings_json.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnExists {
                    conversation_id,
                    slot,
                }
                | TransactionIrStep::TurnRevision {
                    conversation_id,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty() || conversation_id.chars().count() > 256 {
                        return Err(invalid_input("invalid Turn conversation identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnGet {
                    conversation_id,
                    turn_id,
                    slot,
                }
                | TransactionIrStep::TurnImageGet {
                    conversation_id,
                    turn_id,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || turn_id.is_empty()
                        || turn_id.chars().count() > 128
                    {
                        return Err(invalid_input("invalid Turn identity"));
                    }
                    if let TransactionIrStep::TurnImageGet {
                        projection_revision,
                        image_index,
                        ..
                    } = step
                    {
                        if *projection_revision == 0
                            || *projection_revision > i64::MAX as u64
                            || *image_index >= crate::generated_tofudb_ir::MAX_LEGACY_TURN_IMAGES
                        {
                            return Err(invalid_input("invalid legacy Turn image identity"));
                        }
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(turn_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnTimingTraceGet { task_id, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if task_id.is_empty() || task_id.chars().count() > 256 {
                        return Err(invalid_input("invalid timing trace task identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnTimingTraceList {
                    conversation_id,
                    before_created_at,
                    limit,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || before_created_at.is_some_and(|value| value > i64::MAX as u64)
                        || !(1..=crate::generated_tofudb_ir::MAX_TIMING_TRACE_ROWS_PER_QUERY)
                            .contains(limit)
                    {
                        return Err(invalid_input("invalid timing trace list"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnPerceptionRecord { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.turn_id.is_empty()
                        || request.turn_id.chars().count() > 128
                        || request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.recorded_at_ms == 0
                        || !crate::turn::perception_observation_is_valid(
                            &request.observation,
                            &request.attempt_id,
                        )
                    {
                        return Err(invalid_input("invalid Turn perception record"));
                    }
                    let observation_bytes = serde_json::to_vec(&request.observation)
                        .map_err(|_| invalid_input("invalid perception observation"))?
                        .len();
                    literal_bytes = literal_bytes
                        .checked_add(request.conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(request.turn_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.attempt_id.len()))
                        .and_then(|bytes| bytes.checked_add(observation_bytes))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnRecover { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !(1..=crate::generated_tofudb_ir::MAX_RECOVERY_ROWS_PER_TRANSACTION)
                        .contains(&request.max_rows)
                        || !(1..=crate::generated_tofudb_ir::MAX_RECOVERY_PROJECTION_BYTES_PER_TRANSACTION)
                            .contains(&request.max_bytes)
                        || request.now_ms == 0
                        || request
                            .exclude_task_ids
                            .iter()
                            .any(|task_id| task_id.chars().count() > 256)
                    {
                        return Err(invalid_input("invalid Turn recovery step"));
                    }
                    literal_bytes = request.exclude_task_ids.iter().try_fold(
                        literal_bytes,
                        |bytes, task_id| {
                            bytes.checked_add(task_id.len()).ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })
                        },
                    )?;
                    if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
                        return Err(invalid_input("Transaction IR literal bytes exceed 8 MiB"));
                    }
                }
                TransactionIrStep::TurnEventRecord { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.task_id.chars().count() > 256
                        || request.status.is_empty()
                        || request.status.chars().count() > 128
                        || request.event_type.is_empty()
                        || request.event_type.chars().count() > 128
                        || request.now_ms == 0
                        || (request.slim && request.projection_patch.is_some())
                    {
                        return Err(invalid_input("invalid Turn event record"));
                    }
                    if let Some(item) = &request.task_event {
                        if item.task_id.is_empty()
                            || item.task_id.chars().count() > 512
                            || item.event_type.is_empty()
                            || item.event_type.chars().count() > 128
                            || item.payload_json.len()
                                > crate::generated_tofudb_ir::MAX_STREAM_EVENT_BYTES
                        {
                            return Err(invalid_input("invalid carried Turn task event"));
                        }
                    }
                    let request_bytes = serde_json::to_vec(&serde_json::json!({
                        "projection": request.projection,
                        "projectionPatch": request.projection_patch,
                        "settlement": request.settlement,
                        "error": request.error,
                        "eventPayload": request.event_payload,
                        "content": request.content,
                        "thinking": request.thinking,
                    }))
                    .map_err(|_| invalid_input("invalid Turn event record payload"))?
                    .len();
                    literal_bytes = literal_bytes
                        .checked_add(request.attempt_id.len())
                        .and_then(|bytes| bytes.checked_add(request.task_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.status.len()))
                        .and_then(|bytes| bytes.checked_add(request.event_type.len()))
                        .and_then(|bytes| bytes.checked_add(request_bytes))
                        .and_then(|bytes| {
                            bytes.checked_add(
                                request
                                    .task_event
                                    .as_ref()
                                    .map_or(0, |item| item.payload_json.len()),
                            )
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnList {
                    conversation_id,
                    lane_id,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || lane_id
                            .as_ref()
                            .is_some_and(|value| value.is_empty() || value.chars().count() > 256)
                    {
                        return Err(invalid_input("invalid Turn list identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| {
                            bytes.checked_add(lane_id.as_ref().map_or(0, String::len))
                        })
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnListDelta {
                    conversation_id,
                    known_revisions,
                    server_now_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || known_revisions.len() > 2_000
                        || known_revisions
                            .keys()
                            .any(|turn_id| turn_id.is_empty() || turn_id.chars().count() > 128)
                        || *server_now_ms == 0
                    {
                        return Err(invalid_input("invalid Turn delta list"));
                    }
                    literal_bytes = known_revisions.iter().try_fold(
                        literal_bytes
                            .checked_add(conversation_id.len())
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?,
                        |bytes, (turn_id, _)| {
                            bytes.checked_add(turn_id.len()).ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })
                        },
                    )?;
                    if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
                        return Err(invalid_input("Transaction IR literal bytes exceed 8 MiB"));
                    }
                }
                TransactionIrStep::TurnAttemptCreate { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    let valid_target = if request.operation == "regenerate" {
                        matches!(
                            request.target_actor.as_deref(),
                            Some("assistant" | "planner")
                        ) && matches!(
                            request.target_kind.as_deref(),
                            Some("reply" | "plan" | "flow_node")
                        ) && (request.target_actor.as_deref() == Some("planner"))
                            == (request.target_kind.as_deref() == Some("plan"))
                    } else {
                        request.target_actor.is_none() && request.target_kind.is_none()
                    };
                    let answer = request
                        .config
                        .as_object()
                        .and_then(|config| config.get("_humanGuidanceAnswer"));
                    let valid_answer = if request.operation == "answer_guidance" {
                        answer.and_then(Value::as_object).is_some_and(|answer| {
                            answer
                                .get("guidanceId")
                                .and_then(Value::as_str)
                                .is_some_and(|value| {
                                    !value.is_empty() && value.chars().count() <= 128
                                })
                                && answer.get("response").and_then(Value::as_str).is_some_and(
                                    |value| !value.is_empty() && value.chars().count() <= 32_768,
                                )
                        })
                    } else {
                        answer.is_none()
                    };
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.turn_id.is_empty()
                        || request.turn_id.chars().count() > 128
                        || request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.command_id.is_empty()
                        || request.command_id.chars().count() > 256
                        || !matches!(
                            request.operation.as_str(),
                            "continue" | "checkpoint_resume" | "regenerate" | "answer_guidance"
                        )
                        || !matches!(request.dispatch_mode.as_str(), "" | "conversation_executor")
                        || request.created_at_ms == 0
                        || request.committed_at_ms == 0
                        || !valid_target
                        || !valid_answer
                        || request.input_update.is_some()
                            != request.expected_input_projection_revision.is_some()
                        || (request.input_update.is_some() && request.operation != "regenerate")
                    {
                        return Err(invalid_input("invalid Turn attempt create"));
                    }
                    let config_bytes = serde_json::to_vec(&request.config)
                        .map_err(|_| invalid_input("invalid Turn attempt config"))?
                        .len();
                    let anchor_bytes = request
                        .resume_anchor
                        .as_ref()
                        .map(serde_json::to_vec)
                        .transpose()
                        .map_err(|_| invalid_input("invalid Turn attempt anchor"))?
                        .map_or(0, |value| value.len());
                    let update_bytes = request
                        .input_update
                        .as_ref()
                        .map(serde_json::to_vec)
                        .transpose()
                        .map_err(|_| invalid_input("invalid Turn input update"))?
                        .map_or(0, |value| value.len());
                    literal_bytes = [
                        request.conversation_id.len(),
                        request.turn_id.len(),
                        request.attempt_id.len(),
                        request.command_id.len(),
                        request.operation.len(),
                        request.dispatch_mode.len(),
                        request.target_actor.as_ref().map_or(0, String::len),
                        request.target_kind.as_ref().map_or(0, String::len),
                        config_bytes,
                        anchor_bytes,
                        update_bytes,
                    ]
                    .into_iter()
                    .try_fold(literal_bytes, |total, bytes| total.checked_add(bytes))
                    .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                    .ok_or_else(|| invalid_input("Transaction IR literal bytes exceed 8 MiB"))?;
                }
                TransactionIrStep::TurnAttemptDispatchableList {
                    created_before_ms,
                    limit,
                    slot,
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if *created_before_ms
                        > crate::generated_tofudb_ir::MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || !(1..=crate::generated_tofudb_ir::MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY)
                            .contains(limit)
                    {
                        return Err(invalid_input("invalid Turn dispatchable list"));
                    }
                }
                TransactionIrStep::TurnAttemptDispatchWorker { request, slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    let principal_bytes = serde_json::to_vec(&request.principal)
                        .map_err(|_| invalid_input("invalid dispatch principal"))?
                        .len();
                    if request.attempt_id.is_empty()
                        || request.attempt_id.chars().count() > 128
                        || request.user_id == 0
                        || !request.principal.is_object()
                        || request.tenant_label.chars().count() > 256
                        || usize::from(request.priority)
                            > crate::generated_tofudb_ir::MAX_WORKER_JOB_PRIORITY
                        || request.now_ms
                            > crate::generated_tofudb_ir::MAX_WORKER_JOB_CLOCK_MILLISECONDS
                    {
                        return Err(invalid_input("invalid Turn worker dispatch"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(request.attempt_id.len())
                        .and_then(|bytes| bytes.checked_add(request.tenant_label.len()))
                        .and_then(|bytes| bytes.checked_add(principal_bytes))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnAttemptGet { attempt_id, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if attempt_id.is_empty() || attempt_id.chars().count() > 128 {
                        return Err(invalid_input("invalid Turn attempt identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(attempt_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnAttemptClaim {
                    attempt_id,
                    dispatch_owner_id,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if attempt_id.is_empty()
                        || attempt_id.chars().count() > 128
                        || dispatch_owner_id.chars().count() > 64
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn attempt claim"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(attempt_id.len())
                        .and_then(|bytes| bytes.checked_add(dispatch_owner_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnAttemptBind {
                    attempt_id,
                    task_id,
                    dispatch_owner_id,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if attempt_id.is_empty()
                        || attempt_id.chars().count() > 128
                        || task_id.is_empty()
                        || task_id.chars().count() > 256
                        || dispatch_owner_id.chars().count() > 64
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn attempt binding"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(attempt_id.len())
                        .and_then(|bytes| bytes.checked_add(task_id.len()))
                        .and_then(|bytes| bytes.checked_add(dispatch_owner_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnAttemptStart {
                    attempt_id,
                    task_id,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if attempt_id.is_empty()
                        || attempt_id.chars().count() > 128
                        || task_id.is_empty()
                        || task_id.chars().count() > 256
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn attempt start"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(attempt_id.len())
                        .and_then(|bytes| bytes.checked_add(task_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnEventsList {
                    attempt_id,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if attempt_id.is_empty()
                        || attempt_id.chars().count() > 128
                        || !(1..=5_000).contains(limit)
                    {
                        return Err(invalid_input("invalid Turn event list"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(attempt_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnEventsPrune { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.settled_before_ms == 0
                        || !(1..=256).contains(&request.max_attempts)
                        || !(1..=crate::generated_tofudb_ir::MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION)
                            .contains(&request.max_rows)
                    {
                        return Err(invalid_input("invalid Turn event prune"));
                    }
                }
                TransactionIrStep::TurnDelete {
                    conversation_id,
                    turn_ids,
                    deleted_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || turn_ids.is_empty()
                        || turn_ids.len() > 256
                        || turn_ids
                            .iter()
                            .any(|turn_id| turn_id.is_empty() || turn_id.chars().count() > 128)
                        || *deleted_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn delete step"));
                    }
                    literal_bytes = turn_ids.iter().try_fold(
                        literal_bytes
                            .checked_add(conversation_id.len())
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?,
                        |bytes, turn_id| {
                            bytes.checked_add(turn_id.len()).ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })
                        },
                    )?;
                    if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
                        return Err(invalid_input("Transaction IR literal bytes exceed 8 MiB"));
                    }
                }
                TransactionIrStep::TurnCompact { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.conversation_id.is_empty()
                        || request.conversation_id.chars().count() > 256
                        || request.summary_turn_id.is_empty()
                        || request.summary_turn_id.chars().count() > 128
                        || request.delete_turn_ids.len() > 256
                        || request.projection_updates.len()
                            > crate::generated_tofudb_ir::MAX_TURN_COMPACTION_PROJECTION_UPDATES
                        || request.insert_after_turn_id.is_none()
                            && request.insert_before_turn_id.is_none()
                        || request.now_ms == 0
                    {
                        return Err(invalid_input("invalid Turn compaction step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(request.conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(request.summary_turn_id.len()))
                        .and_then(|bytes| bytes.checked_add(request.summary_projection_json.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                    for turn_id in &request.delete_turn_ids {
                        if turn_id.is_empty() || turn_id.chars().count() > 128 {
                            return Err(invalid_input("invalid Turn compaction step"));
                        }
                        literal_bytes =
                            literal_bytes.checked_add(turn_id.len()).ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?;
                    }
                    for update in &request.projection_updates {
                        if update.turn_id.is_empty() || update.turn_id.chars().count() > 128 {
                            return Err(invalid_input("invalid Turn compaction step"));
                        }
                        literal_bytes = literal_bytes
                            .checked_add(update.turn_id.len())
                            .and_then(|bytes| bytes.checked_add(update.projection_json.len()))
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?;
                    }
                    for anchor in [
                        request.insert_after_turn_id.as_deref(),
                        request.insert_before_turn_id.as_deref(),
                    ]
                    .into_iter()
                    .flatten()
                    {
                        if anchor.is_empty() || anchor.chars().count() > 128 {
                            return Err(invalid_input("invalid Turn compaction step"));
                        }
                        literal_bytes =
                            literal_bytes.checked_add(anchor.len()).ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?;
                    }
                    if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
                        return Err(invalid_input("Transaction IR literal bytes exceed 8 MiB"));
                    }
                }
                TransactionIrStep::TurnBranchCreate {
                    conversation_id,
                    parent_turn_id,
                    lane_id,
                    title,
                    kind,
                    anchor_text,
                    parent_selection,
                    expected_projection_revision: _,
                    updated_at_ms,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || parent_turn_id.is_empty()
                        || parent_turn_id.chars().count() > 128
                        || lane_id.is_empty()
                        || lane_id.chars().count() > 128
                        || title.chars().count() > 200
                        || kind.chars().count() > 80
                        || anchor_text.chars().count() > 1_000
                        || parent_selection.chars().count() > 10_000
                        || *updated_at_ms == 0
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn branch create step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(parent_turn_id.len()))
                        .and_then(|bytes| bytes.checked_add(lane_id.len()))
                        .and_then(|bytes| bytes.checked_add(title.len()))
                        .and_then(|bytes| bytes.checked_add(kind.len()))
                        .and_then(|bytes| bytes.checked_add(anchor_text.len()))
                        .and_then(|bytes| bytes.checked_add(parent_selection.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnBranchDelete {
                    conversation_id,
                    parent_turn_id,
                    lane_id,
                    deleted_at_ms,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || parent_turn_id.is_empty()
                        || parent_turn_id.chars().count() > 128
                        || lane_id.is_empty()
                        || lane_id.chars().count() > 128
                        || *deleted_at_ms == 0
                        || *committed_at_ms == 0
                    {
                        return Err(invalid_input("invalid Turn branch delete step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(parent_turn_id.len()))
                        .and_then(|bytes| bytes.checked_add(lane_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnProjectionUpdate {
                    conversation_id,
                    turn_id,
                    projection_json,
                    expected_projection_revision: _,
                    updated_at_ms,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || turn_id.is_empty()
                        || turn_id.chars().count() > 128
                        || *updated_at_ms == 0
                        || *committed_at_ms == 0
                        || serde_json::from_slice::<serde_json::Value>(projection_json)
                            .ok()
                            .and_then(|value| value.as_object().cloned())
                            .is_none()
                    {
                        return Err(invalid_input("invalid Turn projection update step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(turn_id.len()))
                        .and_then(|bytes| bytes.checked_add(projection_json.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                    if literal_bytes > MAX_TRANSACTION_IR_LITERAL_BYTES {
                        return Err(invalid_input("Transaction IR literal bytes exceed 8 MiB"));
                    }
                }
                TransactionIrStep::TurnSyncSnapshot {
                    conversation_id,
                    turn_limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || *turn_limit > 256
                    {
                        return Err(invalid_input("invalid Turn sync snapshot"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnSyncPage {
                    conversation_id,
                    lane_id,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || lane_id.is_empty()
                        || lane_id.chars().count() > 128
                        || !(1..=256).contains(limit)
                    {
                        return Err(invalid_input("invalid Turn sync page"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(lane_id.len()))
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnSyncChanges {
                    conversation_id,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || !(1..=2_000).contains(limit)
                    {
                        return Err(invalid_input("invalid Turn sync changes page"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal bytes exceed 8 MiB")
                        })?;
                }
                TransactionIrStep::TurnSyncPrune { request, slot } => {
                    let initialized = initialized_slots
                        .get_mut(*slot as usize)
                        .ok_or_else(|| invalid_input("Transaction IR slot exceeds its bound"))?;
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.created_before_ms == 0 || !(1..=20_000).contains(&request.max_rows) {
                        return Err(invalid_input("invalid conversation sync prune"));
                    }
                }
            }
            let identity_bytes = match step {
                TransactionIrStep::ArtifactCreate { request, .. } => {
                    if request.artifact_id.is_empty()
                        || request.artifact_id.chars().count() > 256
                        || request.conv_id.is_empty()
                        || request.conv_id.chars().count() > 512
                        || request.task_id.chars().count() > 512
                        || request.msg_id.chars().count() > 512
                        || request.source.is_empty()
                        || request.source.chars().count() > 256
                        || !matches!(request.format.as_str(), "markdown" | "html" | "svg")
                        || request.title.chars().count() > 300
                        || request.parent_id.chars().count() > 256
                        || !request.source_ref.is_object()
                        || !request.meta.is_object()
                        || request.content.len() > crate::generated_tofudb_ir::MAX_ARTIFACT_BYTES
                    {
                        return Err(invalid_input("invalid artifact create step"));
                    }
                    request.artifact_id.len()
                        + request.conv_id.len()
                        + request.task_id.len()
                        + request.msg_id.len()
                        + request.source.len()
                        + request.format.len()
                        + request.title.len()
                        + request.parent_id.len()
                }
                TransactionIrStep::ArtifactGet { artifact_id, .. }
                | TransactionIrStep::ArtifactDelete { artifact_id, .. }
                | TransactionIrStep::ArtifactVersions { artifact_id, .. }
                | TransactionIrStep::ArtifactPin { artifact_id, .. } => {
                    if artifact_id.is_empty() || artifact_id.chars().count() > 256 {
                        return Err(invalid_input("invalid artifact identity step"));
                    }
                    artifact_id.len()
                }
                TransactionIrStep::ArtifactList {
                    conversation_id, ..
                } => {
                    if conversation_id.is_empty() || conversation_id.chars().count() > 512 {
                        return Err(invalid_input("invalid artifact conversation step"));
                    }
                    conversation_id.len()
                }
                TransactionIrStep::ArtifactLibrary { limit, .. } => {
                    if !(1..=200).contains(limit) {
                        return Err(invalid_input("invalid artifact library step"));
                    }
                    0
                }
                TransactionIrStep::ToolResultArtifactPut {
                    content,
                    media_type,
                    created_at_ms,
                    expires_at_ms,
                    ..
                } => {
                    if content.len() > crate::generated_tofudb_ir::MAX_TOOL_RESULT_ARTIFACT_BYTES
                        || media_type.chars().count() > 128
                        || *created_at_ms == 0
                        || *expires_at_ms <= *created_at_ms
                        || expires_at_ms - created_at_ms
                            > crate::generated_tofudb_ir::MAX_TOOL_RESULT_TTL_MILLISECONDS
                    {
                        return Err(invalid_input("invalid tool-result put step"));
                    }
                    // Content is a separately bounded blob input rather than an
                    // inline Transaction IR literal.
                    media_type.len()
                }
                TransactionIrStep::ToolResultArtifactRead {
                    artifact_ref,
                    now_ms,
                    limit,
                    ..
                } => {
                    if artifact_ref.is_empty()
                        || artifact_ref.chars().count() > 96
                        || *now_ms == 0
                        || !(1..=crate::generated_tofudb_ir::MAX_TOOL_RESULT_RANGE_BYTES)
                            .contains(limit)
                    {
                        return Err(invalid_input("invalid tool-result read step"));
                    }
                    artifact_ref.len()
                }
                TransactionIrStep::ToolResultArtifactSearch {
                    artifact_ref,
                    query,
                    now_ms,
                    limit,
                    ..
                } => {
                    if artifact_ref.is_empty()
                        || artifact_ref.chars().count() > 96
                        || query.is_empty()
                        || query.chars().count() > 200
                        || *now_ms == 0
                        || !(1..=20).contains(limit)
                    {
                        return Err(invalid_input("invalid tool-result search step"));
                    }
                    artifact_ref.len() + query.len()
                }
                TransactionIrStep::ToolResultArtifactPrune { now_ms, limit, .. } => {
                    if *now_ms == 0
                        || !(1..=crate::generated_tofudb_ir::MAX_TOOL_RESULT_PRUNE_ROWS)
                            .contains(limit)
                    {
                        return Err(invalid_input("invalid tool-result prune step"));
                    }
                    0
                }
                TransactionIrStep::RateLimitRecordAndCheck { request, .. } => {
                    if !crate::rate_limit::valid_request(request) {
                        return Err(invalid_input("invalid rate-limit admission step"));
                    }
                    request.endpoint.len() + request.client_key.len() + request.event_id.len()
                }
                TransactionIrStep::CompactionArchiveList {
                    conversation_id,
                    limit,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || !(1..=1_000).contains(limit)
                    {
                        return Err(invalid_input("invalid compaction archive list step"));
                    }
                    conversation_id.len()
                }
                TransactionIrStep::CompactionArchiveGet {
                    conversation_id,
                    archive_id,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || archive_id.is_empty()
                        || archive_id.chars().count() > 128
                    {
                        return Err(invalid_input("invalid compaction archive get step"));
                    }
                    conversation_id.len() + archive_id.len()
                }
                TransactionIrStep::CompactionArchiveDeleteConversation {
                    conversation_id, ..
                } => {
                    if conversation_id.is_empty() || conversation_id.chars().count() > 256 {
                        return Err(invalid_input("invalid compaction archive delete step"));
                    }
                    conversation_id.len()
                }
                TransactionIrStep::CompactionArchivePrune {
                    conversation_id,
                    keep,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || !(1..=1_000).contains(keep)
                    {
                        return Err(invalid_input("invalid compaction archive prune step"));
                    }
                    conversation_id.len()
                }
                TransactionIrStep::VersionedDocumentGet {
                    namespace,
                    logical_key,
                    ..
                }
                | TransactionIrStep::VersionedDocumentDelete {
                    namespace,
                    logical_key,
                    ..
                } => namespace.len() + logical_key.len(),
                TransactionIrStep::VersionedDocumentList {
                    namespace, limit, ..
                } => {
                    if *limit == 0 || *limit > MAX_ENTITY_RANGE_ROWS {
                        return Err(invalid_input("Transaction IR document page is unbounded"));
                    }
                    namespace.len()
                }
                TransactionIrStep::IndexedStreamList {
                    task_id,
                    limit,
                    types,
                    type_prefixes,
                    ..
                } => {
                    if *limit == 0
                        || *limit > MAX_ENTITY_RANGE_ROWS
                        || types.len() > 64
                        || type_prefixes.len() > 16
                        || types
                            .iter()
                            .any(|value| value.is_empty() || value.chars().count() > 128)
                        || type_prefixes.iter().any(|value| {
                            value.is_empty()
                                || value.len() > 64
                                || !value.bytes().enumerate().all(|(index, byte)| {
                                    byte.is_ascii_alphanumeric()
                                        || byte == b'_'
                                        || (index > 0 && matches!(byte, b'.' | b':' | b'-'))
                                })
                        })
                    {
                        return Err(invalid_input("Transaction IR event page is unbounded"));
                    }
                    task_id.len()
                        + types.iter().map(String::len).sum::<usize>()
                        + type_prefixes.iter().map(String::len).sum::<usize>()
                }
                TransactionIrStep::IndexedStreamBounds { task_id, .. }
                | TransactionIrStep::IndexedStreamLatest { task_id, .. } => task_id.len(),
                TransactionIrStep::IndexedStreamInspectorSummary { root_task_ids, .. } => {
                    if root_task_ids.is_empty()
                        || root_task_ids.len() > 100
                        || root_task_ids
                            .iter()
                            .any(|task_id| task_id.is_empty() || task_id.chars().count() > 512)
                    {
                        return Err(invalid_input(
                            "Transaction IR inspector task roots are invalid",
                        ));
                    }
                    root_task_ids.iter().map(String::len).sum()
                }
                TransactionIrStep::IndexedStreamPrune {
                    created_before_ms,
                    limit,
                    ..
                } => {
                    if *created_before_ms < 0 || *limit == 0 || *limit > 1_000 {
                        return Err(invalid_input("Transaction IR event prune is unbounded"));
                    }
                    0
                }
                _ => 0,
            };
            literal_bytes = literal_bytes
                .checked_add(identity_bytes)
                .filter(|bytes| *bytes <= MAX_TRANSACTION_IR_LITERAL_BYTES)
                .ok_or_else(|| invalid_input("Transaction IR literal bytes exceed 8 MiB"))?;
        }
        let result_slot = match self.result {
            TransactionIrResult::Literal(_) => None,
            TransactionIrResult::SlotOrLiteral { slot, .. } => Some(slot),
        };
        if result_slot.is_some_and(|slot| {
            !initialized_slots
                .get(slot as usize)
                .copied()
                .unwrap_or(false)
        }) {
            return Err(invalid_input(
                "Transaction IR result references an unread slot",
            ));
        }
        match (self.operation_kind.mutates_state(), &self.command_effects) {
            (false, None) => {
                if self.steps.iter().any(|step| {
                    matches!(step, TransactionIrStep::Queue { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Orchestration { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::IntegrationWorkspace { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Swarm { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Scheduler { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Timer { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Paper { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::PaperLibrary { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::PaperArtifact { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::PaperPodcast { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::RawArchive { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Research { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Optimizer { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::Knowledge { request, .. } if request.mutates_state())
                        || matches!(
                        step,
                        TransactionIrStep::ArtifactCreate { .. }
                            | TransactionIrStep::ArtifactDelete { .. }
                            | TransactionIrStep::ArtifactPin { .. }
                            | TransactionIrStep::ToolResultArtifactPut { .. }
                            | TransactionIrStep::ToolResultArtifactPrune { .. }
                            | TransactionIrStep::RateLimitRecordAndCheck { .. }
                            | TransactionIrStep::CompactionArchiveCreate { .. }
                            | TransactionIrStep::CompactionArchiveUpdateSummary { .. }
                            | TransactionIrStep::CompactionArchiveDeleteConversation { .. }
                            | TransactionIrStep::CompactionArchivePrune { .. }
                            | TransactionIrStep::ConversationCreate { .. }
                            | TransactionIrStep::ConversationClone { .. }
                            | TransactionIrStep::ConversationDelete { .. }
                            | TransactionIrStep::ConversationRestore { .. }
                            | TransactionIrStep::ConversationPurge { .. }
                            | TransactionIrStep::ConversationTrashPrune { .. }
                            | TransactionIrStep::ConversationSettingsUpdate { .. }
                            | TransactionIrStep::ConversationMetadataUpdate { .. }
                            | TransactionIrStep::TurnAppendSettled { .. }
                            | TransactionIrStep::TurnCreatePair { .. }
                            | TransactionIrStep::TurnQueueActivate { .. }
                            | TransactionIrStep::TurnQueueCancel { .. }
                            | TransactionIrStep::TurnSteerCommit { .. }
                            | TransactionIrStep::TurnRelatedAnnounce { .. }
                            | TransactionIrStep::TurnVisibleSync { .. }
                            | TransactionIrStep::TurnAttemptCreate { .. }
                            | TransactionIrStep::TurnAttemptDispatchWorker { .. }
                            | TransactionIrStep::TurnAttemptClaim { .. }
                            | TransactionIrStep::TurnAttemptBind { .. }
                            | TransactionIrStep::TurnAttemptStart { .. }
                            | TransactionIrStep::TurnPerceptionRecord { .. }
                            | TransactionIrStep::TurnRecover { .. }
                            | TransactionIrStep::TurnEventRecord { .. }
                            | TransactionIrStep::TurnEventsPrune { .. }
                            | TransactionIrStep::TurnSyncPrune { .. }
                            | TransactionIrStep::TurnDelete { .. }
                            | TransactionIrStep::TurnCompact { .. }
                            | TransactionIrStep::TurnBranchCreate { .. }
                            | TransactionIrStep::TurnBranchDelete { .. }
                            | TransactionIrStep::TurnProjectionUpdate { .. }
                            | TransactionIrStep::EntityPut { .. }
                            | TransactionIrStep::EntityDelete { .. }
                            | TransactionIrStep::VersionedDocumentPut { .. }
                            | TransactionIrStep::VersionedDocumentDelete { .. }
                            | TransactionIrStep::RecentProjectTouch { .. }
                            | TransactionIrStep::RecentProjectTouchMany { .. }
                            | TransactionIrStep::RecentProjectClear { .. }
                            | TransactionIrStep::ProjectRelink { .. }
                            | TransactionIrStep::DailyCostUpsert { .. }
                            | TransactionIrStep::DailyCostDelete { .. }
                            | TransactionIrStep::ProjectBrainCommand { .. }
                            | TransactionIrStep::ProjectBrainRebuild { .. }
                            | TransactionIrStep::BrowserSiteObservationRecord { .. }
                            | TransactionIrStep::StreamAppend { .. }
                            | TransactionIrStep::IndexedStreamAppend { .. }
                            | TransactionIrStep::IndexedStreamAppendBatch { .. }
                            | TransactionIrStep::IndexedStreamPrune { .. }
                            | TransactionIrStep::TaskResultCheckpoint { .. }
                            | TransactionIrStep::TaskResultAbort { .. }
                            | TransactionIrStep::TaskResultRecoverRunning { .. }
                            | TransactionIrStep::TenantUserCreate { .. }
                            | TransactionIrStep::TenantUserSetStatus { .. }
                            | TransactionIrStep::TenantUserSetRole { .. }
                            | TransactionIrStep::TenantUserRecordLogin { .. }
                            | TransactionIrStep::CredentialCreate { .. }
                            | TransactionIrStep::CredentialCreateIfOwnerEmpty { .. }
                            | TransactionIrStep::CredentialAuthenticate { .. }
                            | TransactionIrStep::CredentialTouch { .. }
                            | TransactionIrStep::CredentialUpdate { .. }
                            | TransactionIrStep::CredentialRevoke { .. }
                            | TransactionIrStep::BillingLedgerAppend { .. }
                            | TransactionIrStep::BillingPaymentRecord { .. }
                            | TransactionIrStep::BillingPaymentSettle { .. }
                            | TransactionIrStep::BillingRedeemCodeApply { .. }
                            | TransactionIrStep::BillingRedeemCodesMint { .. }
                            | TransactionIrStep::BillingWalletApply { .. }
                            | TransactionIrStep::BillingWalletSettle { .. }
                    )
                }) {
                    return Err(invalid_input("Transaction IR query contains writes"));
                }
            }
            (true, Some(effects)) => {
                if effects.command_id.is_empty()
                    || effects.request_id.is_empty()
                    || effects.schema_version == 0
                    || effects.registry_version == 0
                    || effects.committed_at_ms == 0
                    || effects.outbox_payload.len() > MAX_TRANSACTION_IR_LITERAL_BYTES
                {
                    return Err(invalid_input("invalid Transaction IR command effects"));
                }
            }
            (true, None)
                if self.operation == "turn.perception.record"
                    && matches!(
                        self.steps.as_slice(),
                        [TransactionIrStep::TurnPerceptionRecord { .. }]
                    ) => {}
            _ => {
                return Err(invalid_input(
                    "Transaction IR command effects differ from operation kind",
                ));
            }
        }
        Ok(())
    }
}

fn condition_matches(
    condition: EntityWriteCondition,
    slots: &[Option<Option<Vec<u8>>>; MAX_TRANSACTION_IR_SLOTS],
) -> io::Result<bool> {
    match condition {
        EntityWriteCondition::Always => Ok(true),
        EntityWriteCondition::SlotMissing(slot) => slots
            .get(slot as usize)
            .and_then(Option::as_ref)
            .map(|value| value.is_none())
            .ok_or_else(|| invalid_input("Transaction IR condition slot is unavailable")),
        EntityWriteCondition::SlotPresent(slot) => slots
            .get(slot as usize)
            .and_then(Option::as_ref)
            .map(Option::is_some)
            .ok_or_else(|| invalid_input("Transaction IR condition slot is unavailable")),
    }
}

fn result_bytes(
    result: &TransactionIrResult,
    slots: &[Option<Option<Vec<u8>>>; MAX_TRANSACTION_IR_SLOTS],
) -> io::Result<Vec<u8>> {
    match result {
        TransactionIrResult::Literal(value) => Ok(value.clone()),
        TransactionIrResult::SlotOrLiteral { slot, missing } => slots
            .get(*slot as usize)
            .and_then(Option::as_ref)
            .map(|value| value.clone().unwrap_or_else(|| missing.clone()))
            .ok_or_else(|| invalid_input("Transaction IR result slot is unavailable")),
    }
}

fn lookup_receipt(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    ir: &TransactionIr,
) -> io::Result<Option<Vec<u8>>> {
    let Some(effects) = &ir.command_effects else {
        return Ok(None);
    };
    if !effects.store_receipt {
        return Ok(None);
    }
    database.receipt_lookup(
        transaction,
        &effects.command_id,
        ir.operation,
        effects.request_digest,
    )
}

pub fn execute_transaction_ir(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
) -> io::Result<Vec<u8>> {
    ir.validate()?;
    let needs_identity_claim_scopes = ir.steps.iter().any(|step| {
        matches!(
            step,
            TransactionIrStep::RawArchive { .. }
                | TransactionIrStep::CompactionArchiveCreate { .. }
                | TransactionIrStep::Knowledge { .. }
                | TransactionIrStep::CompactionArchiveUpdateSummary { .. }
                | TransactionIrStep::CompactionArchiveDeleteConversation { .. }
                | TransactionIrStep::CompactionArchivePrune { .. }
                | TransactionIrStep::ConversationCreate { .. }
                | TransactionIrStep::ConversationClone { .. }
                | TransactionIrStep::ConversationDelete { .. }
                | TransactionIrStep::ConversationRestore { .. }
                | TransactionIrStep::ConversationPurge { .. }
                | TransactionIrStep::ConversationTrashPrune { .. }
                | TransactionIrStep::TurnAppendSettled { .. }
                | TransactionIrStep::TurnCreatePair { .. }
                | TransactionIrStep::TurnQueueActivate { .. }
                | TransactionIrStep::TurnQueueCancel { .. }
                | TransactionIrStep::TurnSteerCommit { .. }
                | TransactionIrStep::TurnRelatedAnnounce { .. }
                | TransactionIrStep::TurnVisibleSync { .. }
                | TransactionIrStep::TurnAttemptCreate { .. }
                | TransactionIrStep::TurnAttemptDispatchableList { .. }
                | TransactionIrStep::TurnAttemptDispatchWorker { .. }
                | TransactionIrStep::TurnAttemptGet { .. }
                | TransactionIrStep::TurnTimingTraceGet { .. }
                | TransactionIrStep::TurnTimingTraceList { .. }
                | TransactionIrStep::TurnPerceptionRecord { .. }
                | TransactionIrStep::TurnRecover { .. }
                | TransactionIrStep::TurnEventRecord { .. }
                | TransactionIrStep::TurnAttemptClaim { .. }
                | TransactionIrStep::TurnAttemptBind { .. }
                | TransactionIrStep::TurnAttemptStart { .. }
                | TransactionIrStep::TurnEventsList { .. }
                | TransactionIrStep::TurnEventsPrune { .. }
                | TransactionIrStep::TurnSyncSnapshot { .. }
                | TransactionIrStep::TurnSyncPage { .. }
                | TransactionIrStep::TurnDelete { .. }
                | TransactionIrStep::TurnCompact { .. }
                | TransactionIrStep::TurnBranchDelete { .. }
                | TransactionIrStep::ProviderCreate { .. }
                | TransactionIrStep::ProviderDelete { .. }
                | TransactionIrStep::Queue { .. }
                | TransactionIrStep::Orchestration { .. }
                | TransactionIrStep::IntegrationWorkspace { .. }
                | TransactionIrStep::Swarm { .. }
                | TransactionIrStep::Scheduler { .. }
                | TransactionIrStep::Timer { .. }
                | TransactionIrStep::Optimizer { .. }
                | TransactionIrStep::PaperPodcast { .. }
                | TransactionIrStep::WorkerJobEnqueue { .. }
                | TransactionIrStep::WorkerJobGet { .. }
                | TransactionIrStep::WorkerJobClaimNext { .. }
                | TransactionIrStep::WorkerJobHeartbeat { .. }
                | TransactionIrStep::WorkerJobClaimState { .. }
                | TransactionIrStep::WorkerJobRequestCancel { .. }
                | TransactionIrStep::WorkerJobComplete { .. }
                | TransactionIrStep::TaskResultCheckpoint { .. }
                | TransactionIrStep::TaskResultReplayGet { .. }
                | TransactionIrStep::TaskResultAbort { .. }
                | TransactionIrStep::TaskResultAbortRequested { .. }
                | TransactionIrStep::TaskResultRecoverRunning { .. }
                | TransactionIrStep::TaskResultCostExperimentScan { .. }
                | TransactionIrStep::TenantUserCreate { .. }
                | TransactionIrStep::TenantUserGet { .. }
                | TransactionIrStep::TenantUserList { .. }
                | TransactionIrStep::TenantUserSetStatus { .. }
                | TransactionIrStep::TenantUserSetRole { .. }
                | TransactionIrStep::TenantUserAuthentication { .. }
                | TransactionIrStep::TenantUserRecordLogin { .. }
                | TransactionIrStep::CredentialCreate { .. }
                | TransactionIrStep::CredentialCreateIfOwnerEmpty { .. }
                | TransactionIrStep::CredentialGet { .. }
                | TransactionIrStep::CredentialList { .. }
                | TransactionIrStep::CredentialExists { .. }
                | TransactionIrStep::CredentialAuthenticate { .. }
                | TransactionIrStep::CredentialValidate { .. }
                | TransactionIrStep::CredentialIdentify { .. }
                | TransactionIrStep::CredentialTouch { .. }
                | TransactionIrStep::CredentialUpdate { .. }
                | TransactionIrStep::CredentialRevoke { .. }
                | TransactionIrStep::BillingLedgerAppend { .. }
                | TransactionIrStep::BillingLedgerFind { .. }
                | TransactionIrStep::BillingLedgerList { .. }
                | TransactionIrStep::BillingLedgerRecompute { .. }
                | TransactionIrStep::BillingPaymentFind { .. }
                | TransactionIrStep::BillingPaymentList { .. }
                | TransactionIrStep::BillingPaymentRecord { .. }
                | TransactionIrStep::BillingPaymentSettle { .. }
                | TransactionIrStep::BillingRedeemCodeApply { .. }
                | TransactionIrStep::BillingRedeemCodesList { .. }
                | TransactionIrStep::BillingRedeemCodesMint { .. }
                | TransactionIrStep::BillingReserveStale { .. }
                | TransactionIrStep::BillingWalletApply { .. }
                | TransactionIrStep::BillingWalletGet { .. }
                | TransactionIrStep::BillingWalletSettle { .. }
        )
    });
    let mut transaction = if needs_identity_claim_scopes {
        database.begin_with_identity_claim_scopes(ir.tenant_id, ir.owner_user_id)?
    } else {
        database.begin(ir.tenant_id, ir.owner_user_id)?
    };
    if let Some(response) = lookup_receipt(database, &mut transaction, ir)? {
        return Ok(response);
    }
    if ir.operation == "turn.perception.record" {
        database.exempt_content_free_diagnostic_from_logical_outbox(&mut transaction)?;
    }
    let mut slots: [Option<Option<Vec<u8>>>; MAX_TRANSACTION_IR_SLOTS] =
        std::array::from_fn(|_| None);
    for step in &ir.steps {
        match step {
            TransactionIrStep::Queue { request, slot } => {
                slots[*slot as usize] =
                    Some(crate::queue::execute(database, &mut transaction, request)?);
            }
            TransactionIrStep::Orchestration { request, slot } => {
                slots[*slot as usize] = Some(crate::orchestration::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::IntegrationWorkspace { request, slot } => {
                slots[*slot as usize] = Some(crate::integration_workspace::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::Swarm { request, slot } => {
                slots[*slot as usize] =
                    Some(crate::swarm::execute(database, &mut transaction, request)?);
            }
            TransactionIrStep::Scheduler { request, slot } => {
                slots[*slot as usize] = Some(crate::scheduler::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::Timer { request, slot } => {
                slots[*slot as usize] =
                    Some(crate::timer::execute(database, &mut transaction, request)?);
            }
            TransactionIrStep::BillingLedgerAppend { entry, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::ledger_append(
                    database,
                    &mut transaction,
                    entry,
                )?));
            }
            TransactionIrStep::BillingLedgerFind {
                user_id,
                kind,
                ref_type,
                ref_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::billing::ledger_find(
                    database,
                    &mut transaction,
                    user_id,
                    kind,
                    ref_type,
                    ref_id,
                )?);
            }
            TransactionIrStep::BillingLedgerList { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::ledger_list(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::BillingLedgerRecompute { user_id, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::ledger_recompute(
                    database,
                    &mut transaction,
                    user_id,
                )?));
            }
            TransactionIrStep::BillingPaymentFind {
                provider,
                provider_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::billing::payment_find(
                    database,
                    &mut transaction,
                    provider,
                    provider_id,
                )?);
            }
            TransactionIrStep::BillingPaymentList { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::payment_list(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::BillingPaymentRecord {
                provider,
                provider_id,
                payload_json,
                created_at,
                physical_updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::billing::payment_record(
                    database,
                    &mut transaction,
                    provider,
                    provider_id,
                    payload_json,
                    *created_at,
                    *physical_updated_at_ms,
                )?));
            }
            TransactionIrStep::BillingPaymentSettle {
                payment_id,
                payload_json,
                settled_at,
                physical_updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::billing::payment_settle(
                    database,
                    &mut transaction,
                    payment_id,
                    payload_json,
                    *settled_at,
                    *physical_updated_at_ms,
                )?));
            }
            TransactionIrStep::BillingRedeemCodeApply { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::redeem_code_apply(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::BillingRedeemCodesList { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::redeem_codes_list(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::BillingRedeemCodesMint { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::redeem_codes_mint(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::BillingReserveStale {
                cutoff_ts,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::billing::reserve_stale(
                    database,
                    &mut transaction,
                    *cutoff_ts,
                    *limit,
                )?));
            }
            TransactionIrStep::BillingWalletApply { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::wallet_apply(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::BillingWalletGet { user_id, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::wallet_get(
                    database,
                    &mut transaction,
                    user_id,
                )?));
            }
            TransactionIrStep::BillingWalletSettle { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::billing::wallet_settle(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::ArtifactCreate { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::artifact::create(
                    database,
                    &mut transaction,
                    request.clone(),
                )?));
            }
            TransactionIrStep::ArtifactGet {
                artifact_id,
                include_content,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::artifact::get(
                    database,
                    &mut transaction,
                    artifact_id,
                    *include_content,
                )?);
            }
            TransactionIrStep::ArtifactList {
                conversation_id,
                include_deleted,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::artifact::list(
                    database,
                    &mut transaction,
                    conversation_id,
                    *include_deleted,
                )?));
            }
            TransactionIrStep::ArtifactDelete {
                artifact_id,
                deleted_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::artifact::delete(
                    database,
                    &mut transaction,
                    artifact_id,
                    *deleted_at_ms,
                )?));
            }
            TransactionIrStep::ArtifactVersions { artifact_id, slot } => {
                slots[*slot as usize] = Some(Some(crate::artifact::versions(
                    database,
                    &mut transaction,
                    artifact_id,
                )?));
            }
            TransactionIrStep::ArtifactLibrary { limit, slot } => {
                slots[*slot as usize] = Some(Some(crate::artifact::library(
                    database,
                    &mut transaction,
                    *limit,
                )?));
            }
            TransactionIrStep::ArtifactPin {
                artifact_id,
                pinned,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::artifact::pin(
                    database,
                    &mut transaction,
                    artifact_id,
                    *pinned,
                )?));
            }
            TransactionIrStep::ToolResultArtifactPut {
                content,
                media_type,
                created_at_ms,
                expires_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::artifact::tool_put(
                    database,
                    &mut transaction,
                    content,
                    media_type,
                    *created_at_ms,
                    *expires_at_ms,
                )?));
            }
            TransactionIrStep::ToolResultArtifactRead {
                artifact_ref,
                now_ms,
                offset,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::artifact::tool_read(
                    database,
                    &mut transaction,
                    artifact_ref,
                    *now_ms,
                    *offset,
                    *limit,
                )?);
            }
            TransactionIrStep::ToolResultArtifactSearch {
                artifact_ref,
                query,
                now_ms,
                cursor,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::artifact::tool_search(
                    database,
                    &mut transaction,
                    artifact_ref,
                    query,
                    *now_ms,
                    *cursor,
                    *limit,
                )?);
            }
            TransactionIrStep::ToolResultArtifactPrune {
                now_ms,
                limit,
                slot,
            } => {
                let progress =
                    crate::artifact::tool_prune(database, &mut transaction, *now_ms, *limit)?;
                slots[*slot as usize] = Some(Some(
                    serde_json::to_vec(&serde_json::json!({
                        "deleted": progress.deleted,
                        "hasMore": progress.has_more,
                    }))
                    .map_err(|_| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "tool-result prune response cannot be encoded",
                        )
                    })?,
                ));
            }
            TransactionIrStep::SystemSchemaVersion { slot } => {
                slots[*slot as usize] = Some(Some(
                    serde_json::to_vec(&serde_json::json!({
                        "version": crate::generated_storage_operations::STORAGE_SCHEMA_VERSION
                    }))
                    .map_err(|_| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "schema version response cannot be encoded",
                        )
                    })?,
                ));
            }
            TransactionIrStep::RateLimitRecordAndCheck { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::rate_limit::record_and_check(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::CompactionArchiveCreate {
                archive_id,
                conversation_id,
                messages_json,
                summary,
                receipt_json,
                trigger,
                task_id,
                round_num,
                model,
                tokens_before,
                tokens_after,
                msgs_before,
                msgs_after,
                reason,
                created_at_ms,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::create(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::CreateRequest {
                        archive_id: archive_id.clone(),
                        conversation_id: conversation_id.clone(),
                        messages_json: messages_json.clone(),
                        summary: summary.clone(),
                        receipt_json: receipt_json.clone(),
                        trigger: trigger.clone(),
                        task_id: task_id.clone(),
                        round_num: *round_num,
                        model: model.clone(),
                        tokens_before: *tokens_before,
                        tokens_after: *tokens_after,
                        msgs_before: *msgs_before,
                        msgs_after: *msgs_after,
                        reason: reason.clone(),
                        created_at_ms: *created_at_ms,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::CompactionArchiveList {
                conversation_id,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::list(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::ListRequest {
                        conversation_id: conversation_id.clone(),
                        limit: *limit,
                    },
                )?));
            }
            TransactionIrStep::CompactionArchiveGet {
                conversation_id,
                archive_id,
                include_messages,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::compaction_archive::get(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::GetRequest {
                        conversation_id: conversation_id.clone(),
                        archive_id: archive_id.clone(),
                        include_messages: *include_messages,
                    },
                )?);
            }
            TransactionIrStep::CompactionArchiveUpdateSummary {
                archive_id,
                summary,
                tokens_after,
                msgs_after,
                receipt_json,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::update_summary(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::UpdateSummaryRequest {
                        archive_id: archive_id.clone(),
                        summary: summary.clone(),
                        tokens_after: *tokens_after,
                        msgs_after: *msgs_after,
                        receipt_json: receipt_json.clone(),
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::CompactionArchiveDeleteConversation {
                conversation_id,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::delete_conversation(
                    database,
                    &mut transaction,
                    conversation_id,
                )?));
            }
            TransactionIrStep::CompactionArchivePrune {
                conversation_id,
                keep,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::prune(
                    database,
                    &mut transaction,
                    conversation_id,
                    *keep,
                )?));
            }
            TransactionIrStep::ConversationCreate {
                conversation_id,
                title,
                settings_json,
                created_at_ms,
                updated_at_ms,
                committed_at_ms,
            } => {
                crate::conversation_header::create(
                    database,
                    &mut transaction,
                    &crate::conversation_header::CreateRequest {
                        conversation_id: conversation_id.clone(),
                        title: title.clone(),
                        settings_json: settings_json.clone(),
                        created_at_ms: *created_at_ms,
                        updated_at_ms: *updated_at_ms,
                        committed_at_ms: *committed_at_ms,
                    },
                )?;
            }
            TransactionIrStep::ConversationClone {
                source_conversation_id,
                destination_conversation_id,
                title,
                identity_seed,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::clone_conversation(
                    database,
                    &mut transaction,
                    &crate::conversation_header::CloneRequest {
                        source_conversation_id: source_conversation_id.clone(),
                        destination_conversation_id: destination_conversation_id.clone(),
                        title: title.clone(),
                        identity_seed: *identity_seed,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::ConversationDelete {
                conversation_id,
                deleted_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::delete(
                    database,
                    &mut transaction,
                    &crate::conversation_header::DeleteRequest {
                        conversation_id: conversation_id.clone(),
                        deleted_at_ms: *deleted_at_ms,
                    },
                )?));
            }
            TransactionIrStep::ConversationRestore {
                conversation_id,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::restore(
                    database,
                    &mut transaction,
                    &crate::conversation_header::RestoreRequest {
                        conversation_id: conversation_id.clone(),
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::ConversationPurge {
                conversation_id,
                purged_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::purge(
                    database,
                    &mut transaction,
                    &crate::conversation_header::PurgeRequest {
                        conversation_id: conversation_id.clone(),
                        purged_at_ms: *purged_at_ms,
                    },
                )?));
            }
            TransactionIrStep::ConversationTrashPrune {
                deleted_before_ms,
                maximum_conversations,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::trash_prune(
                    database,
                    &mut transaction,
                    &crate::conversation_header::TrashPruneRequest {
                        deleted_before_ms: *deleted_before_ms,
                        maximum_conversations: *maximum_conversations,
                    },
                )?));
            }
            TransactionIrStep::ConversationCount { slot } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::count(
                    database,
                    &mut transaction,
                )?));
            }
            TransactionIrStep::ConversationActivityDates {
                updated_at_gte,
                created_at_lt,
                day_boundaries_ms,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::activity_dates(
                    database,
                    &mut transaction,
                    &crate::conversation_header::ActivityDatesRequest {
                        updated_at_gte: *updated_at_gte,
                        created_at_lt: *created_at_lt,
                        day_boundaries_ms: day_boundaries_ms.clone(),
                        limit: *limit,
                    },
                )?));
            }
            TransactionIrStep::ConversationGet {
                conversation_id,
                include_messages,
                message_window,
                before_sequence,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::conversation_header::get(
                    database,
                    &mut transaction,
                    conversation_id,
                    *include_messages,
                    *message_window,
                    *before_sequence,
                )?);
            }
            TransactionIrStep::ConversationCatalogPage {
                folder_id,
                before_updated_at_ms,
                before_id,
                limit,
                settings_keys,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::catalog_page(
                    database,
                    &mut transaction,
                    &crate::conversation_header::CatalogPageRequest {
                        folder_id: folder_id.clone(),
                        before_updated_at_ms: *before_updated_at_ms,
                        before_id: before_id.clone(),
                        limit: *limit,
                        settings_keys: settings_keys.clone(),
                    },
                )?));
            }
            TransactionIrStep::ConversationList {
                project_path,
                title_contains,
                ids,
                updated_at_gte,
                updated_at_gt,
                created_at_lt,
                updated_descending,
                settings_keys,
                include_messages,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::list(
                    database,
                    &mut transaction,
                    &crate::conversation_header::ListRequest {
                        project_path: project_path.clone(),
                        title_contains: title_contains.clone(),
                        ids: ids.clone(),
                        updated_at_gte: *updated_at_gte,
                        updated_at_gt: *updated_at_gt,
                        created_at_lt: *created_at_lt,
                        order: if *updated_descending {
                            crate::conversation_header::ListOrder::UpdatedDescending
                        } else {
                            crate::conversation_header::ListOrder::IdAscending
                        },
                        settings_keys: settings_keys.clone(),
                        include_messages: *include_messages,
                        limit: *limit,
                    },
                )?));
            }
            TransactionIrStep::ModelRoutingGet { tenant_label, slot } => {
                slots[*slot as usize] = Some(crate::model_routing::get(
                    database,
                    &mut transaction,
                    tenant_label,
                )?);
            }
            TransactionIrStep::ModelRoutingCommit {
                tenant_label,
                expected_revision,
                document_json,
                migration_receipt_json,
                updated_at,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::model_routing::commit(
                    database,
                    &mut transaction,
                    crate::model_routing::CommitRequest {
                        tenant_label: tenant_label.clone(),
                        expected_revision: *expected_revision,
                        document: serde_json::from_slice(document_json).map_err(|_| {
                            invalid_input("model-routing authority document is not JSON")
                        })?,
                        migration_receipt: migration_receipt_json
                            .as_ref()
                            .map(|value| serde_json::from_slice(value))
                            .transpose()
                            .map_err(|_| {
                                invalid_input("model-routing migration receipt is not JSON")
                            })?,
                        updated_at: *updated_at,
                    },
                )?));
            }
            TransactionIrStep::ModelRoutingMigrationReceiptGet { tenant_label, slot } => {
                slots[*slot as usize] = Some(crate::model_routing::migration_receipt(
                    database,
                    &mut transaction,
                    tenant_label,
                )?);
            }
            TransactionIrStep::ModelRoutingMigrationReceiptPut {
                tenant_label,
                receipt_json,
                initial_document_json,
                updated_at,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::model_routing::put_migration_receipt(
                    database,
                    &mut transaction,
                    tenant_label,
                    serde_json::from_slice(receipt_json)
                        .map_err(|_| invalid_input("model-routing receipt is not JSON"))?,
                    initial_document_json
                        .as_ref()
                        .map(|value| serde_json::from_slice(value))
                        .transpose()
                        .map_err(|_| invalid_input("initial model-routing document is not JSON"))?,
                    *updated_at,
                )?));
            }
            TransactionIrStep::ModelRoutingSecretPut {
                tenant_label,
                secret_reference,
                ciphertext,
                key_hint,
                updated_at,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::model_routing::secret_put(
                    database,
                    &mut transaction,
                    tenant_label,
                    secret_reference,
                    ciphertext,
                    key_hint,
                    *updated_at,
                )?));
            }
            TransactionIrStep::ModelRoutingSecretGet {
                tenant_label,
                secret_reference,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::model_routing::secret_get(
                    database,
                    &mut transaction,
                    tenant_label,
                    secret_reference,
                )?);
            }
            TransactionIrStep::ModelRoutingSecretList { tenant_label, slot } => {
                slots[*slot as usize] = Some(Some(crate::model_routing::secret_list(
                    database,
                    &mut transaction,
                    tenant_label,
                )?));
            }
            TransactionIrStep::ModelRoutingSecretDelete {
                tenant_label,
                secret_reference,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::model_routing::secret_delete(
                    database,
                    &mut transaction,
                    tenant_label,
                    secret_reference,
                )?));
            }
            TransactionIrStep::ModelRoutingSecretPrune {
                tenant_label,
                active_secret_references,
                updated_before,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::model_routing::secret_prune(
                    database,
                    &mut transaction,
                    tenant_label,
                    active_secret_references,
                    *updated_before,
                )?));
            }
            TransactionIrStep::ProviderCreate {
                tenant_label,
                provider_id,
                document_json,
                physical_updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::provider::create(
                    database,
                    &mut transaction,
                    tenant_label,
                    provider_id,
                    document_json,
                    *physical_updated_at_ms,
                )?));
            }
            TransactionIrStep::ProviderGet {
                tenant_label,
                provider_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::provider::get(
                    database,
                    &mut transaction,
                    tenant_label,
                    provider_id,
                )?);
            }
            TransactionIrStep::ProviderList { tenant_label, slot } => {
                slots[*slot as usize] = Some(Some(crate::provider::list(
                    database,
                    &mut transaction,
                    tenant_label,
                )?));
            }
            TransactionIrStep::ProviderUpdate {
                tenant_label,
                provider_id,
                updates_json,
                updated_at,
                physical_updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::provider::update(
                    database,
                    &mut transaction,
                    tenant_label,
                    provider_id,
                    updates_json,
                    *updated_at,
                    *physical_updated_at_ms,
                )?);
            }
            TransactionIrStep::ProviderDelete {
                tenant_label,
                provider_id,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::provider::delete(
                    database,
                    &mut transaction,
                    tenant_label,
                    provider_id,
                )?));
            }
            TransactionIrStep::ProviderTouch {
                tenant_label,
                provider_id,
                used_at,
                physical_updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::provider::touch(
                    database,
                    &mut transaction,
                    tenant_label,
                    provider_id,
                    *used_at,
                    *physical_updated_at_ms,
                )?));
            }
            TransactionIrStep::WorkerJobEnqueue {
                task_id,
                user_id,
                tenant_label,
                task_kind,
                payload_json,
                idempotency_key,
                request_digest,
                priority,
                available_at_ms,
                now_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::worker_job::enqueue(
                    database,
                    &mut transaction,
                    crate::worker_job::EnqueueRequest {
                        task_id: task_id.clone(),
                        user_id: *user_id,
                        tenant_id: tenant_label.clone(),
                        task_kind: task_kind.clone(),
                        payload: serde_json::from_slice(payload_json)
                            .map_err(|_| invalid_input("worker job payload is not JSON"))?,
                        idempotency_key: idempotency_key.clone(),
                        request_digest: request_digest.clone(),
                        priority: *priority,
                        available_at_ms: *available_at_ms,
                        now_ms: *now_ms,
                    },
                )?));
            }
            TransactionIrStep::WorkerJobGet {
                task_id,
                user_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::worker_job::get(
                    database,
                    &mut transaction,
                    task_id,
                    *user_id,
                )?);
            }
            TransactionIrStep::WorkerJobClaimNext {
                worker_id,
                now_ms,
                lease_ms,
                task_kinds,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::worker_job::claim_next(
                    database,
                    &mut transaction,
                    worker_id,
                    *now_ms,
                    *lease_ms,
                    task_kinds,
                )?);
            }
            TransactionIrStep::WorkerJobHeartbeat {
                task_id,
                worker_id,
                fencing_token,
                now_ms,
                lease_ms,
                replay_cursor,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::worker_job::heartbeat(
                    database,
                    &mut transaction,
                    crate::worker_job::HeartbeatRequest {
                        task_id,
                        worker_id,
                        fence: *fencing_token,
                        now_ms: *now_ms,
                        lease_ms: *lease_ms,
                        replay_cursor: *replay_cursor,
                    },
                )?));
            }
            TransactionIrStep::WorkerJobClaimState {
                task_id,
                worker_id,
                fencing_token,
                now_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::worker_job::claim_state(
                    database,
                    &mut transaction,
                    task_id,
                    worker_id,
                    *fencing_token,
                    *now_ms,
                )?));
            }
            TransactionIrStep::WorkerJobRequestCancel {
                task_id,
                user_id,
                now_ms,
                reason,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::worker_job::request_cancel(
                    database,
                    &mut transaction,
                    task_id,
                    *user_id,
                    *now_ms,
                    reason,
                )?);
            }
            TransactionIrStep::WorkerJobComplete {
                task_id,
                worker_id,
                fencing_token,
                now_ms,
                terminal_status,
                result_ref,
                replay_cursor,
                error_json,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::worker_job::complete(
                    database,
                    &mut transaction,
                    crate::worker_job::CompleteRequest {
                        task_id,
                        worker_id,
                        fence: *fencing_token,
                        now_ms: *now_ms,
                        terminal_status,
                        result_ref,
                        replay_cursor: *replay_cursor,
                        error: serde_json::from_slice(error_json)
                            .map_err(|_| invalid_input("worker job error is not JSON"))?,
                    },
                )?));
            }
            TransactionIrStep::ConversationSettingsUpdate {
                conversation_id,
                updates_json,
                replace,
                expected_settings_json,
                expected_revision,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::update_settings(
                    database,
                    &mut transaction,
                    &crate::conversation_header::SettingsUpdateRequest {
                        conversation_id: conversation_id.clone(),
                        updates_json: updates_json.clone(),
                        replace: *replace,
                        expected_settings_json: expected_settings_json.clone(),
                        expected_revision: *expected_revision,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::ConversationMetadataUpdate {
                conversation_id,
                title,
                updated_at_ms,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::conversation_header::update_metadata(
                    database,
                    &mut transaction,
                    &crate::conversation_header::MetadataUpdateRequest {
                        conversation_id: conversation_id.clone(),
                        title: title.clone(),
                        updated_at_ms: *updated_at_ms,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::TurnAppendSettled {
                conversation_id,
                actor,
                status,
                projection_json,
                settlement_json,
                lane_id,
                command_id,
                kind,
                run_id,
                turn_id,
                attempt_id,
                created_at_ms,
                committed_at_ms,
                allow_create,
                default_title,
                default_settings_json,
                default_created_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::append_settled(
                    database,
                    &mut transaction,
                    &crate::turn::AppendSettledRequest {
                        conversation_id: conversation_id.clone(),
                        actor: actor.clone(),
                        status: status.clone(),
                        projection_json: projection_json.clone(),
                        settlement_json: settlement_json.clone(),
                        lane_id: lane_id.clone(),
                        command_id: command_id.clone(),
                        kind: kind.clone(),
                        run_id: run_id.clone(),
                        turn_id: turn_id.clone(),
                        attempt_id: attempt_id.clone(),
                        created_at_ms: *created_at_ms,
                        committed_at_ms: *committed_at_ms,
                        defaults: crate::conversation_header::TurnDefaults {
                            allow_create: *allow_create,
                            title: default_title.clone(),
                            settings_json: default_settings_json.clone(),
                            created_at_ms: *default_created_at_ms,
                        },
                    },
                )?));
            }
            TransactionIrStep::TurnCreatePair { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::create_pair(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnQueueActivate { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::queue_activate(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnQueueCancel { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::queue_cancel(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnSteerCommit { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::steer_commit(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnRelatedAnnounce { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::related_announce(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnVisibleSync { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::visible_sync(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnExists {
                conversation_id,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::exists(
                    database,
                    &mut transaction,
                    conversation_id,
                )?));
            }
            TransactionIrStep::TurnGet {
                conversation_id,
                turn_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::get(
                    database,
                    &mut transaction,
                    conversation_id,
                    turn_id,
                )?);
            }
            TransactionIrStep::TurnImageGet {
                conversation_id,
                turn_id,
                projection_revision,
                image_index,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::image_get(
                    database,
                    &mut transaction,
                    conversation_id,
                    turn_id,
                    *projection_revision,
                    *image_index,
                )?);
            }
            TransactionIrStep::TurnList {
                conversation_id,
                lane_id,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::list(
                    database,
                    &mut transaction,
                    conversation_id,
                    lane_id.as_deref(),
                )?));
            }
            TransactionIrStep::TurnListDelta {
                conversation_id,
                since_ms,
                known_revisions,
                server_now_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::list_delta(
                    database,
                    &mut transaction,
                    conversation_id,
                    *since_ms,
                    known_revisions,
                    *server_now_ms,
                )?));
            }
            TransactionIrStep::TurnRevision {
                conversation_id,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::revision(
                    database,
                    &mut transaction,
                    conversation_id,
                )?));
            }
            TransactionIrStep::TurnAttemptCreate { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_create(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnAttemptDispatchableList {
                created_before_ms,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_dispatchable_list(
                    database,
                    &mut transaction,
                    *created_before_ms,
                    *limit,
                )?));
            }
            TransactionIrStep::TurnAttemptDispatchWorker { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_dispatch_worker(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnAttemptGet { attempt_id, slot } => {
                slots[*slot as usize] = Some(crate::turn::attempt_get(
                    database,
                    &mut transaction,
                    attempt_id,
                )?);
            }
            TransactionIrStep::TurnTimingTraceGet { task_id, slot } => {
                slots[*slot as usize] = Some(crate::turn::timing_trace_get(
                    database,
                    &mut transaction,
                    task_id,
                )?);
            }
            TransactionIrStep::TurnTimingTraceList {
                conversation_id,
                before_created_at,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::timing_trace_list(
                    database,
                    &mut transaction,
                    conversation_id,
                    *before_created_at,
                    *limit,
                )?));
            }
            TransactionIrStep::TurnPerceptionRecord { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::perception_record(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnRecover { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::recover(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnEventRecord { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::event_record(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnAttemptClaim {
                attempt_id,
                dispatch_owner_id,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_claim(
                    database,
                    &mut transaction,
                    attempt_id,
                    dispatch_owner_id,
                    *committed_at_ms,
                )?));
            }
            TransactionIrStep::TurnAttemptBind {
                attempt_id,
                task_id,
                dispatch_owner_id,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_bind(
                    database,
                    &mut transaction,
                    attempt_id,
                    task_id,
                    dispatch_owner_id,
                    *committed_at_ms,
                )?));
            }
            TransactionIrStep::TurnAttemptStart {
                attempt_id,
                task_id,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_start(
                    database,
                    &mut transaction,
                    attempt_id,
                    task_id,
                    *committed_at_ms,
                )?));
            }
            TransactionIrStep::TurnEventsList {
                attempt_id,
                after,
                limit,
                patch_mode,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::events_list(
                    database,
                    &mut transaction,
                    attempt_id,
                    *after,
                    *limit,
                    *patch_mode,
                )?);
            }
            TransactionIrStep::TurnEventsPrune { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::events_prune(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnDelete {
                conversation_id,
                turn_ids,
                deleted_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::delete(
                    database,
                    &mut transaction,
                    conversation_id,
                    turn_ids,
                    *deleted_at_ms,
                )?));
            }
            TransactionIrStep::TurnCompact { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::compact(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnBranchCreate {
                conversation_id,
                parent_turn_id,
                lane_id,
                title,
                kind,
                anchor_text,
                parent_selection,
                expected_projection_revision,
                updated_at_ms,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::branch_create(
                    database,
                    &mut transaction,
                    &crate::turn::BranchCreateRequest {
                        conversation_id: conversation_id.clone(),
                        parent_turn_id: parent_turn_id.clone(),
                        lane_id: lane_id.clone(),
                        title: title.clone(),
                        kind: kind.clone(),
                        anchor_text: anchor_text.clone(),
                        parent_selection: parent_selection.clone(),
                        expected_projection_revision: *expected_projection_revision,
                        updated_at_ms: *updated_at_ms,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::TurnBranchDelete {
                conversation_id,
                parent_turn_id,
                lane_id,
                deleted_at_ms,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::branch_delete(
                    database,
                    &mut transaction,
                    &crate::turn::BranchDeleteRequest {
                        conversation_id: conversation_id.clone(),
                        parent_turn_id: parent_turn_id.clone(),
                        lane_id: lane_id.clone(),
                        deleted_at_ms: *deleted_at_ms,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::TurnProjectionUpdate {
                conversation_id,
                turn_id,
                projection_json,
                expected_projection_revision,
                updated_at_ms,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::update_projection(
                    database,
                    &mut transaction,
                    &crate::turn::ProjectionUpdateRequest {
                        conversation_id: conversation_id.clone(),
                        turn_id: turn_id.clone(),
                        projection_json: projection_json.clone(),
                        expected_projection_revision: *expected_projection_revision,
                        updated_at_ms: *updated_at_ms,
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::TurnSyncSnapshot {
                conversation_id,
                turn_limit,
                include_artifact_hint,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::sync_snapshot(
                    database,
                    &mut transaction,
                    conversation_id,
                    *turn_limit,
                    *include_artifact_hint,
                )?);
            }
            TransactionIrStep::TurnSyncPage {
                conversation_id,
                lane_id,
                before_ordinal,
                limit,
                sync_sequence,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::sync_page(
                    database,
                    &mut transaction,
                    conversation_id,
                    lane_id,
                    *before_ordinal,
                    *limit,
                    *sync_sequence,
                )?);
            }
            TransactionIrStep::TurnSyncChanges {
                conversation_id,
                after,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::sync_changes(
                    database,
                    &mut transaction,
                    conversation_id,
                    *after,
                    *limit,
                )?);
            }
            TransactionIrStep::TurnSyncPrune { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::sync_prune(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::EntityRead { key, slot } => {
                slots[*slot as usize] = Some(database.entity_get(&mut transaction, key)?);
            }
            TransactionIrStep::EntityPut {
                key,
                value,
                condition,
            } => {
                if condition_matches(*condition, &slots)? {
                    database.entity_put(&mut transaction, key.clone(), value.clone())?;
                }
            }
            TransactionIrStep::EntityDelete { key, condition } => {
                if condition_matches(*condition, &slots)? {
                    database.entity_delete(&mut transaction, key.clone())?;
                }
            }
            TransactionIrStep::VersionedDocumentGet {
                key,
                namespace,
                logical_key,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::versioned_document::get(
                    database,
                    &mut transaction,
                    key,
                    namespace,
                    logical_key,
                )?);
            }
            TransactionIrStep::TaskResultCheckpoint {
                task_id,
                value_json,
                expected_version,
                updated_at_ms,
                guarded,
                require_parent,
                cache_prefix_hwm,
                last_turn_cache_read,
                slot,
            } => {
                let guard = guarded.then_some(crate::task_result::CheckpointGuard {
                    require_parent: *require_parent,
                    cache_prefix_hwm: *cache_prefix_hwm,
                    last_turn_cache_read: *last_turn_cache_read,
                });
                slots[*slot as usize] = Some(Some(crate::task_result::checkpoint(
                    database,
                    &mut transaction,
                    task_id,
                    value_json.clone(),
                    *expected_version,
                    *updated_at_ms,
                    guard.as_ref(),
                )?));
            }
            TransactionIrStep::TaskResultReplayGet {
                task_id,
                requested_user_id,
                include_terminal_payload,
                include_metadata,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::task_result::replay_get(
                    database,
                    &mut transaction,
                    task_id,
                    *requested_user_id,
                    *include_terminal_payload,
                    *include_metadata,
                )?);
            }
            TransactionIrStep::TaskResultSummaryList {
                requested_user_id,
                status,
                conversation_id,
                completed_before_ms,
                limit,
                scan_limit,
                order_by,
                after_key,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::task_result::summary_list(
                    database,
                    &mut transaction,
                    &crate::task_result::SummaryListRequest {
                        status: status.clone(),
                        requested_user_id: *requested_user_id,
                        conversation_id: conversation_id.clone(),
                        completed_before_ms: *completed_before_ms,
                        limit: *limit,
                        scan_limit: *scan_limit,
                        order_by: order_by.clone(),
                        after_key: after_key.clone(),
                    },
                )?));
            }
            TransactionIrStep::TaskResultAbort {
                task_id,
                requested_user_id,
                source,
                requested_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::task_result::abort(
                    database,
                    &mut transaction,
                    task_id,
                    *requested_user_id,
                    source,
                    *requested_at_ms,
                )?));
            }
            TransactionIrStep::TaskResultAbortRequested {
                task_id,
                requested_user_id,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::task_result::abort_requested(
                    database,
                    &mut transaction,
                    task_id,
                    *requested_user_id,
                )?));
            }
            TransactionIrStep::TaskResultRecoverRunning {
                interrupted_reason,
                maximum_rows,
                scan_limit,
                updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::task_result::recover_running(
                    database,
                    &mut transaction,
                    &crate::task_result::RecoverRunningRequest {
                        interrupted_reason: interrupted_reason.clone(),
                        maximum_rows: *maximum_rows,
                        scan_limit: *scan_limit,
                        updated_at_ms: *updated_at_ms,
                    },
                )?));
            }
            TransactionIrStep::TaskResultCostExperimentScan {
                requested_user_id,
                experiment_id,
                completed_at_gte,
                limit,
                scan_limit,
                after_key,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::task_result::cost_experiment_scan(
                    database,
                    &mut transaction,
                    &crate::task_result::CostExperimentScanRequest {
                        requested_user_id: *requested_user_id,
                        experiment_id: experiment_id.clone(),
                        completed_at_gte: *completed_at_gte,
                        limit: *limit,
                        scan_limit: *scan_limit,
                        after_key: after_key.clone(),
                    },
                )?));
            }
            TransactionIrStep::TenantUserCreate { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::tenant_user::create(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TenantUserGet { selector, slot } => {
                slots[*slot as usize] = Some(crate::tenant_user::get(
                    database,
                    &mut transaction,
                    selector,
                )?);
            }
            TransactionIrStep::TenantUserList { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::tenant_user::list(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TenantUserSetStatus {
                user_id,
                status,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::tenant_user::set_status(
                    database,
                    &mut transaction,
                    user_id,
                    status,
                )?);
            }
            TransactionIrStep::TenantUserSetRole {
                user_id,
                role,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::tenant_user::set_role(
                    database,
                    &mut transaction,
                    user_id,
                    role,
                )?);
            }
            TransactionIrStep::TenantUserAuthentication { email, slot } => {
                slots[*slot as usize] = Some(crate::tenant_user::authentication(
                    database,
                    &mut transaction,
                    email,
                )?);
            }
            TransactionIrStep::TenantUserRecordLogin {
                user_id,
                last_login_at,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::tenant_user::record_login(
                    database,
                    &mut transaction,
                    user_id,
                    *last_login_at,
                )?));
            }
            TransactionIrStep::CredentialCreate { request, slot } => {
                slots[*slot as usize] = Some(crate::credential::create(
                    database,
                    &mut transaction,
                    request,
                    false,
                )?);
            }
            TransactionIrStep::CredentialCreateIfOwnerEmpty { request, slot } => {
                slots[*slot as usize] = Some(crate::credential::create(
                    database,
                    &mut transaction,
                    request,
                    true,
                )?);
            }
            TransactionIrStep::CredentialGet {
                boundary,
                credential_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::credential::get(
                    database,
                    &mut transaction,
                    boundary,
                    credential_id,
                )?);
            }
            TransactionIrStep::CredentialList { boundary, slot } => {
                slots[*slot as usize] = Some(Some(crate::credential::list(
                    database,
                    &mut transaction,
                    boundary,
                )?));
            }
            TransactionIrStep::CredentialExists { boundary, slot } => {
                slots[*slot as usize] = Some(Some(crate::credential::exists(
                    database,
                    &mut transaction,
                    boundary,
                )?));
            }
            TransactionIrStep::CredentialAuthenticate {
                secret_hash,
                now,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::credential::authenticate(
                    database,
                    &mut transaction,
                    secret_hash,
                    *now,
                )?);
            }
            TransactionIrStep::CredentialValidate {
                secret_hash,
                now,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::credential::validate(
                    database,
                    &mut transaction,
                    secret_hash,
                    *now,
                )?);
            }
            TransactionIrStep::CredentialIdentify { secret_hash, slot } => {
                slots[*slot as usize] = Some(crate::credential::identify(
                    database,
                    &mut transaction,
                    secret_hash,
                )?);
            }
            TransactionIrStep::CredentialTouch {
                boundary,
                credential_id,
                used_at,
                touch_if_before,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::credential::touch(
                    database,
                    &mut transaction,
                    boundary,
                    credential_id,
                    *used_at,
                    *touch_if_before,
                )?));
            }
            TransactionIrStep::CredentialUpdate {
                boundary,
                credential_id,
                request,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::credential::update(
                    database,
                    &mut transaction,
                    boundary,
                    credential_id,
                    request,
                )?);
            }
            TransactionIrStep::CredentialRevoke {
                boundary,
                credential_id,
                revoked_at,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::credential::revoke(
                    database,
                    &mut transaction,
                    boundary,
                    credential_id,
                    *revoked_at,
                )?));
            }
            TransactionIrStep::VersionedDocumentList {
                start,
                end,
                namespace,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::versioned_document::list(
                    database,
                    &mut transaction,
                    start,
                    end,
                    namespace,
                    *limit,
                )?));
            }
            TransactionIrStep::VersionedDocumentPut {
                key,
                namespace,
                logical_key,
                value_json,
                expected_version,
                updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::versioned_document::put(
                    database,
                    &mut transaction,
                    crate::versioned_document::PutRequest {
                        key: key.clone(),
                        namespace: namespace.clone(),
                        logical_key: logical_key.clone(),
                        value_json: value_json.clone(),
                        expected_version: *expected_version,
                        updated_at_ms: *updated_at_ms,
                    },
                )?));
            }
            TransactionIrStep::VersionedDocumentDelete {
                key,
                namespace,
                logical_key,
                expected_version,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::versioned_document::delete(
                    database,
                    &mut transaction,
                    key.clone(),
                    namespace,
                    logical_key,
                    *expected_version,
                )?));
            }
            TransactionIrStep::RecentProjectList { slot } => {
                slots[*slot as usize] = Some(Some(crate::recent_project::list(
                    database,
                    &mut transaction,
                )?));
            }
            TransactionIrStep::RecentProjectTouch {
                path,
                last_used,
                updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::recent_project::touch(
                    database,
                    &mut transaction,
                    path,
                    *last_used,
                    *updated_at_ms,
                )?));
            }
            TransactionIrStep::RecentProjectTouchMany {
                paths,
                last_used,
                updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::recent_project::touch_many(
                    database,
                    &mut transaction,
                    paths,
                    *last_used,
                    *updated_at_ms,
                )?));
            }
            TransactionIrStep::RecentProjectClear { slot } => {
                slots[*slot as usize] = Some(Some(crate::recent_project::clear(
                    database,
                    &mut transaction,
                )?));
            }
            TransactionIrStep::ProjectRelink {
                old_path,
                new_path,
                updated_at_ms,
                slot,
            } => {
                let project = crate::recent_project::relink(
                    database,
                    &mut transaction,
                    old_path,
                    new_path,
                    *updated_at_ms,
                )?;
                let brain_moved = crate::project_brain::relink_scope(
                    database,
                    &mut transaction,
                    old_path,
                    new_path,
                    *updated_at_ms,
                )?;
                let (conversations_moved, trashed_conversations_moved) =
                    crate::conversation_header::relink_project_settings(
                        database,
                        &mut transaction,
                        old_path,
                        new_path,
                        *updated_at_ms,
                    )?;
                slots[*slot as usize] = Some(Some(
                    serde_json::to_vec(&json!({
                        "project": project,
                        "projectBrainMoved": brain_moved,
                        "conversationsMoved": conversations_moved,
                        "trashedConversationsMoved": trashed_conversations_moved,
                    }))
                    .map_err(|_| {
                        io::Error::new(
                            io::ErrorKind::InvalidData,
                            "project relink response cannot be encoded",
                        )
                    })?,
                ));
            }
            TransactionIrStep::DailyCostMonth { year, month, slot } => {
                slots[*slot as usize] = Some(Some(crate::daily_cost::month(
                    database,
                    &mut transaction,
                    *year,
                    *month,
                )?));
            }
            TransactionIrStep::DailyCostLatest { slot } => {
                slots[*slot as usize] =
                    Some(crate::daily_cost::latest(database, &mut transaction)?);
            }
            TransactionIrStep::DailyCostPersistedDates { dates, slot } => {
                slots[*slot as usize] = Some(Some(crate::daily_cost::persisted_dates(
                    database,
                    &mut transaction,
                    dates,
                )?));
            }
            TransactionIrStep::DailyCostUpsert {
                date,
                cost,
                conversations_json,
                computed_at,
                updated_at_ms,
                slot,
            } => {
                let conversations = serde_json::from_slice(conversations_json)
                    .ok()
                    .and_then(|value: serde_json::Value| value.as_object().cloned())
                    .ok_or_else(|| invalid_input("daily-cost conversations are malformed"))?;
                slots[*slot as usize] = Some(Some(crate::daily_cost::upsert(
                    database,
                    &mut transaction,
                    &crate::daily_cost::UpsertRequest {
                        date: date.clone(),
                        cost: *cost,
                        conversations,
                        computed_at: *computed_at,
                        updated_at_ms: *updated_at_ms,
                    },
                )?));
            }
            TransactionIrStep::DailyCostDelete { date, slot } => {
                slots[*slot as usize] = Some(Some(crate::daily_cost::delete(
                    database,
                    &mut transaction,
                    date.as_deref(),
                )?));
            }
            TransactionIrStep::LogAggregateFlush {
                rows_json,
                cutoff_ms,
                updated_at_ms,
                slot,
            } => {
                let rows: Vec<crate::log_aggregate::FlushRow> =
                    serde_json::from_slice(rows_json)
                        .map_err(|_| invalid_input("log-aggregate flush rows are malformed"))?;
                slots[*slot as usize] = Some(Some(crate::log_aggregate::flush(
                    database,
                    &mut transaction,
                    &rows,
                    *cutoff_ms,
                    *updated_at_ms,
                )?));
            }
            TransactionIrStep::LogAggregateQuery {
                level,
                q,
                sort,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::log_aggregate::query(
                    database,
                    &mut transaction,
                    level,
                    q,
                    *sort,
                    *limit,
                )?));
            }
            TransactionIrStep::PluginRegister {
                manifest_json,
                updated_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::plugin::register(
                    database,
                    &mut transaction,
                    manifest_json,
                    *updated_at_ms,
                )?));
            }
            TransactionIrStep::PluginManifestGet { namespace, slot } => {
                slots[*slot as usize] = Some(Some(crate::plugin::manifest_get(
                    database,
                    &mut transaction,
                    namespace,
                )?));
            }
            TransactionIrStep::Paper { request, slot } => {
                slots[*slot as usize] =
                    Some(crate::paper::execute(database, &mut transaction, request)?);
            }
            TransactionIrStep::PaperLibrary { request, slot } => {
                slots[*slot as usize] = Some(crate::paper_library::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::PaperArtifact { request, slot } => {
                slots[*slot as usize] = Some(crate::paper_artifact::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::PaperPodcast { request, slot } => {
                slots[*slot as usize] = Some(crate::paper_podcast::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::RawArchive { request, slot } => {
                slots[*slot as usize] = Some(crate::raw_archive::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::Research { request, slot } => {
                slots[*slot as usize] = Some(crate::research::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::Optimizer { request, slot } => {
                slots[*slot as usize] = Some(crate::optimizer::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::Knowledge { request, slot } => {
                slots[*slot as usize] = Some(crate::knowledge::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::ProjectBrainGet { project_key, slot } => {
                slots[*slot as usize] = Some(Some(crate::project_brain::get(
                    database,
                    &mut transaction,
                    project_key,
                )?));
            }
            TransactionIrStep::ProjectBrainActiveList { slot } => {
                slots[*slot as usize] = Some(Some(crate::project_brain::list_active(
                    database,
                    &mut transaction,
                )?));
            }
            TransactionIrStep::ProjectBrainRecoverySnapshot { slot } => {
                slots[*slot as usize] = Some(Some(crate::project_brain::recovery_snapshot(
                    database,
                    &mut transaction,
                )?));
            }
            TransactionIrStep::ProjectBrainRebuild {
                project_key,
                timestamp,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::project_brain::rebuild(
                    database,
                    &mut transaction,
                    project_key,
                    *timestamp,
                )?));
            }
            TransactionIrStep::ProjectBrainCommand {
                project_key,
                action,
                payload_json,
                timestamp,
                slot,
            } => {
                let payload = serde_json::from_slice(payload_json)
                    .ok()
                    .and_then(|value: serde_json::Value| value.as_object().cloned())
                    .ok_or_else(|| invalid_input("Project Brain payload is malformed"))?;
                slots[*slot as usize] = Some(Some(crate::project_brain::command(
                    database,
                    &mut transaction,
                    &crate::project_brain::CommandRequest {
                        project_key: project_key.clone(),
                        action: *action,
                        payload,
                        timestamp: *timestamp,
                    },
                )?));
            }
            TransactionIrStep::BrowserSiteObservationGet {
                origin,
                route_family,
                operation,
                now_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::browser_site_observation::get(
                    database,
                    &mut transaction,
                    &crate::browser_site_observation::Identity {
                        origin: origin.clone(),
                        route_family: route_family.clone(),
                        operation: operation.clone(),
                    },
                    *now_ms,
                )?);
            }
            TransactionIrStep::BrowserSiteObservationRecord {
                origin,
                route_family,
                operation,
                outcome,
                observed_at_ms,
                observation_json,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::browser_site_observation::record(
                    database,
                    &mut transaction,
                    &crate::browser_site_observation::RecordRequest {
                        identity: crate::browser_site_observation::Identity {
                            origin: origin.clone(),
                            route_family: route_family.clone(),
                            operation: operation.clone(),
                        },
                        outcome: outcome.clone(),
                        observed_at_ms: *observed_at_ms,
                        observation_json: observation_json.clone(),
                    },
                    *committed_at_ms,
                )?);
            }
            TransactionIrStep::StreamAppend {
                key,
                expected_next_sequence,
                events,
            } => {
                database.stream_append(
                    &mut transaction,
                    key.clone(),
                    *expected_next_sequence,
                    events.clone(),
                )?;
            }
            TransactionIrStep::IndexedStreamAppend {
                task_id,
                application_sequence,
                event_type,
                payload_json,
                created_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::indexed_stream::append(
                    database,
                    &mut transaction,
                    &crate::indexed_stream::AppendRequest {
                        task_id: task_id.clone(),
                        application_sequence: *application_sequence,
                        event_type: event_type.clone(),
                        payload_json: payload_json.clone(),
                        created_at_ms: *created_at_ms,
                    },
                )?));
            }
            TransactionIrStep::IndexedStreamAppendBatch { items, slot } => {
                let requests = items
                    .iter()
                    .map(|item| crate::indexed_stream::AppendRequest {
                        task_id: item.task_id.clone(),
                        application_sequence: item.application_sequence,
                        event_type: item.event_type.clone(),
                        payload_json: item.payload_json.clone(),
                        created_at_ms: item.created_at_ms,
                    })
                    .collect::<Vec<_>>();
                slots[*slot as usize] = Some(Some(crate::indexed_stream::append_batch(
                    database,
                    &mut transaction,
                    &requests,
                )?));
            }
            TransactionIrStep::IndexedStreamList {
                task_id,
                after_sequence,
                limit,
                types,
                type_prefixes,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::indexed_stream::list(
                    database,
                    &mut transaction,
                    &crate::indexed_stream::ListRequest {
                        task_id: task_id.clone(),
                        after_sequence: *after_sequence,
                        limit: *limit,
                        types: types.clone(),
                        type_prefixes: type_prefixes.clone(),
                    },
                )?));
            }
            TransactionIrStep::IndexedStreamBounds { task_id, slot } => {
                slots[*slot as usize] = Some(Some(crate::indexed_stream::bounds(
                    database,
                    &mut transaction,
                    task_id,
                )?));
            }
            TransactionIrStep::IndexedStreamInspectorSummary {
                root_task_ids,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::indexed_stream::inspector_summary(
                    database,
                    &mut transaction,
                    root_task_ids,
                )?));
            }
            TransactionIrStep::IndexedStreamPrune {
                created_before_ms,
                limit,
                retention_class,
                slot,
            } => {
                let retention_class = match retention_class {
                    IndexedStreamRetentionClass::Streaming => {
                        crate::indexed_stream::RetentionClass::Streaming
                    }
                    IndexedStreamRetentionClass::Structural => {
                        crate::indexed_stream::RetentionClass::Structural
                    }
                };
                slots[*slot as usize] = Some(Some(crate::indexed_stream::prune(
                    database,
                    &mut transaction,
                    &crate::indexed_stream::PruneRequest {
                        created_before_ms: *created_before_ms,
                        limit: *limit,
                        retention_class,
                    },
                )?));
            }
            TransactionIrStep::IndexedStreamLatest { task_id, slot } => {
                slots[*slot as usize] = Some(crate::indexed_stream::latest(
                    database,
                    &mut transaction,
                    task_id,
                )?);
            }
        }
    }
    let response = result_bytes(&ir.result, &slots)?;
    let Some(effects) = &ir.command_effects else {
        if transaction.has_business_mutation() {
            database.commit(transaction)?;
        }
        return Ok(response);
    };
    let receipt_cacheable = effects.store_receipt
        && serde_json::from_slice::<serde_json::Value>(&response).map_or(true, |value| {
            !value.is_null()
                && !value
                    .as_object()
                    .is_some_and(|object| object.get("ok") == Some(&serde_json::Value::Bool(false)))
        });
    if !receipt_cacheable && !transaction.has_business_mutation() {
        return Ok(response);
    }
    if receipt_cacheable {
        database.receipt_insert(
            &mut transaction,
            &effects.command_id,
            ir.operation,
            effects.request_digest,
            &response,
            effects.committed_at_ms,
        )?;
    }
    if database.logical_outbox_is_configured() {
        database.logical_outbox_capture(
            &mut transaction,
            LogicalOutboxCapture {
                schema_version: effects.schema_version,
                registry_version: effects.registry_version,
                operation: ir.operation.to_owned(),
                request_id: effects.request_id.clone(),
                request_digest: effects.request_digest,
                command_id: Some(effects.command_id.clone()),
                committed_at_ms: effects.committed_at_ms,
                clear_payload: effects.outbox_payload.clone(),
            },
        )?;
    }
    database.commit(transaction)?;
    Ok(response)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> EntityKey {
        EntityKey::new(7, 11, "ir-test", b"key").unwrap()
    }

    fn query(steps: Vec<TransactionIrStep>, result: TransactionIrResult) -> TransactionIr {
        TransactionIr {
            tenant_id: 7,
            owner_user_id: 11,
            operation: "record.get",
            operation_kind: StorageOperationKind::Query,
            steps,
            result,
            command_effects: None,
        }
    }

    fn stream_command(key: StreamKey) -> TransactionIr {
        TransactionIr {
            tenant_id: 7,
            owner_user_id: 11,
            operation: "event.append",
            operation_kind: StorageOperationKind::Command,
            steps: vec![TransactionIrStep::StreamAppend {
                key,
                expected_next_sequence: 1,
                events: vec![
                    StreamEvent::new(2_000, "delta", br#"{"text":"x"}"#.to_vec()).unwrap(),
                ],
            }],
            result: TransactionIrResult::Literal(br#"{"inserted":true}"#.to_vec()),
            command_effects: Some(TransactionIrCommandEffects {
                command_id: "natural-event-key".to_owned(),
                request_digest: [4; 32],
                request_id: "request-event-1".to_owned(),
                schema_version: 57,
                registry_version: 37,
                committed_at_ms: 2_000,
                outbox_payload: br#"{"operation":"event.append"}"#.to_vec(),
                store_receipt: false,
            }),
        }
    }

    #[test]
    fn validation_rejects_query_writes_and_uninitialized_slots() {
        assert!(query(
            vec![TransactionIrStep::EntityPut {
                key: key(),
                value: b"value".to_vec(),
                condition: EntityWriteCondition::Always,
            }],
            TransactionIrResult::Literal(b"result".to_vec()),
        )
        .validate()
        .is_err());
        assert!(query(
            Vec::new(),
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"missing".to_vec(),
            },
        )
        .validate()
        .is_err());
        assert!(query(
            vec![TransactionIrStep::EntityPut {
                key: key(),
                value: b"value".to_vec(),
                condition: EntityWriteCondition::SlotMissing(0),
            }],
            TransactionIrResult::Literal(b"result".to_vec()),
        )
        .validate()
        .is_err());
    }

    #[test]
    fn explicit_entity_owner_scope_is_rejected_before_interpretation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let ir = query(
            vec![TransactionIrStep::EntityRead {
                key: EntityKey::new(7, 12, "ir-test", b"foreign").unwrap(),
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"missing".to_vec(),
            },
        );
        assert_eq!(
            execute_transaction_ir(&mut database, &ir)
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidInput
        );
    }

    #[test]
    fn stream_append_uses_the_same_atomic_ir_effect_boundary() {
        let directory = tempfile::tempdir().unwrap();
        let key = StreamKey::new(7, 11, "task_event", b"task-1").unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            database
                .configure_logical_outbox(&[55; 32], 1024 * 1024, 128 * 1024)
                .unwrap();
            assert_eq!(
                execute_transaction_ir(&mut database, &stream_command(key.clone())).unwrap(),
                br#"{"inserted":true}"#
            );
            assert_eq!(
                database.logical_outbox_status(7, 11).unwrap().last_sequence,
                1
            );
        }
        let mut reopened = AuthorityDatabase::open(directory.path()).unwrap();
        reopened
            .configure_logical_outbox(&[55; 32], 1024 * 1024, 128 * 1024)
            .unwrap();
        let page = reopened.stream_read(7, 11, &key, 1, 10).unwrap();
        assert_eq!(page.events.len(), 1);
        assert_eq!(page.events[0].event.payload, br#"{"text":"x"}"#);
        assert_eq!(
            reopened.logical_outbox_status(7, 11).unwrap().last_sequence,
            1
        );
    }
}
