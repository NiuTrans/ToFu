//! Bounded, owner-scoped Transaction IR and its sole authority interpreter.
//!
//! Semantic compilers produce this concrete IR after validating their wire
//! payload. The interpreter opens one OCC transaction, evaluates reads and
//! conditional writes in order, derives the response, and atomically attaches
//! the command receipt and logical outbox record. No compiler receives direct
//! access to physical pages, WAL records, or blob paths. Private IrResponse
//! certificates follow actual immutable output; any mutable slot access invalidates
//! them. execute_transaction_ir_with_json_proof exposes this to semantic validation.

mod slots;
use slots::SparseSlotValues;

use std::io;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use crate::authority::{AuthorityDatabase, AuthorityTransaction};
use crate::entity::EntityKey;
use crate::generated_storage_operations::StorageOperationKind;
pub use crate::generated_tofudb_ir::{
    MAX_ENTITY_RANGE_ROWS,
};
use crate::generated_tofudb_ir::{
    MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS,
    MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS,
    MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS, MAX_MODEL_ROUTING_TIMESTAMP_SECONDS,
    MAX_PROVIDER_TENANT_LABEL_CHARACTERS, MAX_RECENT_PROJECT_PATH_CHARACTERS,
    MAX_TASK_RESULT_CACHE_FACT,
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
    SlotMissing(usize),
    SlotPresent(usize),
}

#[derive(Clone, Debug)]
pub struct IndexedStreamAppendItem {
    pub task_id: String,
    pub application_sequence: u64,
    pub event_type: String,
    pub payload_json: Vec<u8>,
    pub created_at_ms: i64,
    /// Conversation-attempt fence: when non-empty, the append is rejected
    /// with `attempt_not_live` unless every listed attempt still exists with
    /// a live status. Enforced inside the append transaction so a streaming
    /// producer needs no separate authoritative pre-read per visible frame
    /// (2026-09-18 stream-churn fix; mirrors the SQL sidecar's
    /// `require_attempts_live` in `_records._append_event_row`).
    pub require_attempts_live: Vec<String>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IndexedStreamRetentionClass {
    Streaming,
    Structural,
}

/// The attempt fence travels with high-rate streamed frames, so its bounds
/// must stay cheap to validate: at most one attempt plus one flow root,
/// each an ordinary attempt-id string. Mirrors
/// `lib/tasks_pkg/event_log._normalize_require_attempts_live`.
fn validate_attempt_fence(require_attempts_live: &[String]) -> io::Result<()> {
    if require_attempts_live.len() > 2
        || require_attempts_live
            .iter()
            .any(|attempt_id| attempt_id.is_empty() || attempt_id.chars().count() > 128)
    {
        return Err(invalid_input("invalid indexed stream attempt fence"));
    }
    Ok(())
}

/// One standalone stream append with its conversation-attempt fence applied
/// inside the same transaction. A fenced-stale row is skipped, not an error:
/// the batch sibling rows and the aggregate response must survive so one
/// settled attempt cannot poison unrelated streams sharing a batch.
fn execute_fenced_indexed_append(
    database: &AuthorityDatabase,
    transaction: &mut AuthorityTransaction,
    request: &crate::indexed_stream::AppendRequest,
    require_attempts_live: &[String],
) -> io::Result<Vec<u8>> {
    if !require_attempts_live.is_empty()
        && !crate::turn::attempts_all_live(database, transaction, require_attempts_live)?
    {
        return serde_json::to_vec(&json!({
            "inserted": false,
            "rejected": "attempt_not_live",
            "task_id": request.task_id,
            "sequence": request.application_sequence,
        }))
        .map_err(|_| invalid_input("indexed event fence rejection cannot be encoded"));
    }
    crate::indexed_stream::append(database, transaction, request)
}

#[derive(Clone, Debug)]
pub enum TransactionIrStep {
    BillingLedgerAppend {
        entry: crate::billing::LedgerEntry,
        slot: usize,
    },
    BillingLedgerFind {
        user_id: String,
        kind: String,
        ref_type: String,
        ref_id: String,
        slot: usize,
    },
    BillingLedgerList {
        request: crate::billing::LedgerListRequest,
        slot: usize,
    },
    BillingLedgerRecompute {
        user_id: String,
        slot: usize,
    },
    BillingPaymentFind {
        provider: String,
        provider_id: String,
        slot: usize,
    },
    BillingPaymentList {
        request: crate::billing::PaymentListRequest,
        slot: usize,
    },
    BillingPaymentRecord {
        provider: String,
        provider_id: String,
        payload_json: Vec<u8>,
        created_at: u64,
        physical_updated_at_ms: u64,
        slot: usize,
    },
    BillingPaymentSettle {
        payment_id: String,
        payload_json: Vec<u8>,
        settled_at: u64,
        physical_updated_at_ms: u64,
        slot: usize,
    },
    BillingRedeemCodeApply {
        request: crate::billing::RedeemApplyRequest,
        slot: usize,
    },
    BillingRedeemCodesList {
        request: crate::billing::RedeemListRequest,
        slot: usize,
    },
    BillingRedeemCodesMint {
        request: crate::billing::RedeemMintRequest,
        slot: usize,
    },
    BillingReserveStale {
        cutoff_ts: u64,
        limit: usize,
        slot: usize,
    },
    BillingWalletApply {
        request: crate::billing::WalletApplyRequest,
        slot: usize,
    },
    BillingWalletGet {
        user_id: String,
        slot: usize,
    },
    BillingWalletSettle {
        request: crate::billing::WalletSettleRequest,
        slot: usize,
    },
    ArtifactCreate {
        request: crate::artifact::CreateRequest,
        slot: usize,
    },
    ArtifactGet {
        artifact_id: String,
        include_content: bool,
        slot: usize,
    },
    ArtifactList {
        conversation_id: String,
        include_deleted: bool,
        slot: usize,
    },
    ArtifactDelete {
        artifact_id: String,
        deleted_at_ms: u64,
        slot: usize,
    },
    ArtifactVersions {
        artifact_id: String,
        slot: usize,
    },
    ArtifactLibrary {
        limit: usize,
        slot: usize,
    },
    ArtifactPin {
        artifact_id: String,
        pinned: bool,
        slot: usize,
    },
    ToolResultArtifactPut {
        content: String,
        media_type: String,
        created_at_ms: u64,
        expires_at_ms: u64,
        slot: usize,
    },
    ToolResultArtifactRead {
        artifact_ref: String,
        now_ms: u64,
        offset: serde_json::Value,
        selector: String,
        limit: usize,
        slot: usize,
    },
    ToolResultArtifactSearch {
        artifact_ref: String,
        query: String,
        now_ms: u64,
        cursor: serde_json::Value,
        selector: String,
        limit: usize,
        slot: usize,
    },
    ToolResultArtifactPrune {
        now_ms: u64,
        limit: usize,
        slot: usize,
    },
    SystemSchemaVersion {
        slot: usize,
    },
    RateLimitRecordAndCheck {
        request: crate::rate_limit::RecordAndCheckRequest,
        slot: usize,
    },
    CompactionArchiveCreate {
        archive_id: String,
        conversation_id: String,
        archive_scope: String,
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
        slot: usize,
    },
    CompactionArchiveList {
        conversation_id: String,
        archive_scope: String,
        limit: usize,
        slot: usize,
    },
    CompactionArchiveGet {
        conversation_id: String,
        archive_id: String,
        archive_scope: String,
        include_messages: bool,
        include_post_messages: bool,
        slot: usize,
    },
    CompactionArchiveUpdateSummary {
        archive_id: String,
        archive_scope: String,
        summary: String,
        tokens_after: u64,
        msgs_after: u64,
        receipt_json: Option<Vec<u8>>,
        post_messages_json: Option<Vec<u8>>,
        committed_at_ms: u64,
        slot: usize,
    },
    CompactionArchiveDeleteConversation {
        conversation_id: String,
        archive_scope: String,
        slot: usize,
    },
    CompactionArchivePrune {
        conversation_id: String,
        archive_scope: String,
        keep: usize,
        slot: usize,
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
        slot: usize,
    },
    ConversationDelete {
        conversation_id: String,
        deleted_at_ms: u64,
        slot: usize,
    },
    ConversationRestore {
        conversation_id: String,
        committed_at_ms: u64,
        slot: usize,
    },
    ConversationPurge {
        conversation_id: String,
        purged_at_ms: u64,
        slot: usize,
    },
    ConversationTrashPrune {
        deleted_before_ms: u64,
        maximum_conversations: usize,
        slot: usize,
    },
    ConversationCount {
        slot: usize,
    },
    ConversationActivityDates {
        updated_at_gte: i64,
        created_at_lt: Option<i64>,
        day_boundaries_ms: Vec<i64>,
        limit: usize,
        slot: usize,
    },
    ConversationDiagnostic {
        request: crate::turn::diagnostic::Request,
        slot: usize,
    },
    ConversationGet {
        conversation_id: String,
        include_messages: bool,
        message_window: usize,
        before_sequence: Option<i64>,
        slot: usize,
    },
    ConversationSettingsUpdate {
        conversation_id: String,
        updates_json: Vec<u8>,
        replace: bool,
        expected_settings_json: Option<Vec<u8>>,
        expected_revision: Option<u64>,
        committed_at_ms: u64,
        slot: usize,
    },
    ConversationMetadataUpdate {
        conversation_id: String,
        title: Option<String>,
        updated_at_ms: Option<u64>,
        committed_at_ms: u64,
        slot: usize,
    },
    ConversationCatalogPage {
        folder_id: Option<String>,
        before_updated_at_ms: Option<u64>,
        before_id: String,
        limit: usize,
        settings_keys: Option<Vec<String>>,
        slot: usize,
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
        slot: usize,
    },
    ModelRoutingGet {
        tenant_label: String,
        slot: usize,
    },
    ModelRoutingCommit {
        tenant_label: String,
        expected_revision: u64,
        document_json: Vec<u8>,
        migration_receipt_json: Option<Vec<u8>>,
        updated_at: f64,
        slot: usize,
    },
    ModelRoutingMigrationReceiptGet {
        tenant_label: String,
        slot: usize,
    },
    ModelRoutingMigrationReceiptPut {
        tenant_label: String,
        receipt_json: Vec<u8>,
        initial_document_json: Option<Vec<u8>>,
        updated_at: f64,
        slot: usize,
    },
    ModelRoutingSecretPut {
        tenant_label: String,
        secret_reference: String,
        ciphertext: String,
        key_hint: String,
        updated_at: f64,
        slot: usize,
    },
    ModelRoutingSecretGet {
        tenant_label: String,
        secret_reference: String,
        slot: usize,
    },
    ModelRoutingSecretList {
        tenant_label: String,
        slot: usize,
    },
    ModelRoutingSecretDelete {
        tenant_label: String,
        secret_reference: String,
        slot: usize,
    },
    ModelRoutingSecretPrune {
        tenant_label: String,
        active_secret_references: std::collections::BTreeSet<String>,
        updated_before: f64,
        slot: usize,
    },
    ProviderCreate {
        tenant_label: String,
        provider_id: String,
        document_json: Vec<u8>,
        physical_updated_at_ms: u64,
        slot: usize,
    },
    ProviderGet {
        tenant_label: String,
        provider_id: String,
        slot: usize,
    },
    ProviderList {
        tenant_label: String,
        slot: usize,
    },
    ProviderUpdate {
        tenant_label: String,
        provider_id: String,
        updates_json: Vec<u8>,
        updated_at: f64,
        physical_updated_at_ms: u64,
        slot: usize,
    },
    ProviderDelete {
        tenant_label: String,
        provider_id: String,
        slot: usize,
    },
    ProviderTouch {
        tenant_label: String,
        provider_id: String,
        used_at: f64,
        physical_updated_at_ms: u64,
        slot: usize,
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
        slot: usize,
    },
    TaskResultRecordGet {
        task_id: String,
        requested_user_id: u64,
        slot: usize,
    },
    TaskResultReplayGet {
        task_id: String,
        requested_user_id: u64,
        include_terminal_payload: bool,
        include_metadata: bool,
        slot: usize,
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
        slot: usize,
    },
    TaskResultAbort {
        task_id: String,
        requested_user_id: u64,
        source: String,
        requested_at_ms: u64,
        slot: usize,
    },
    TaskResultAbortRequested {
        task_id: String,
        requested_user_id: u64,
        slot: usize,
    },
    TaskResultRecoverRunning {
        interrupted_reason: String,
        maximum_rows: usize,
        scan_limit: usize,
        updated_at_ms: u64,
        slot: usize,
    },
    TaskResultRecoveryPending {
        slot: usize,
    },
    TaskResultCostExperimentScan {
        requested_user_id: u64,
        experiment_id: String,
        completed_at_gte: i64,
        limit: usize,
        scan_limit: usize,
        after_key: String,
        slot: usize,
    },
    TenantUserCreate {
        request: crate::tenant_user::CreateRequest,
        slot: usize,
    },
    TenantUserGet {
        selector: crate::tenant_user::Selector,
        slot: usize,
    },
    TenantUserList {
        request: crate::tenant_user::ListRequest,
        slot: usize,
    },
    TenantUserSetStatus {
        user_id: String,
        status: String,
        slot: usize,
    },
    TenantUserSetRole {
        user_id: String,
        role: String,
        slot: usize,
    },
    TenantUserAuthentication {
        email: String,
        slot: usize,
    },
    TenantUserRecordLogin {
        user_id: String,
        last_login_at: u64,
        slot: usize,
    },
    CredentialCreate {
        request: crate::credential::CreateRequest,
        slot: usize,
    },
    CredentialCreateIfOwnerEmpty {
        request: crate::credential::CreateRequest,
        slot: usize,
    },
    CredentialGet {
        boundary: crate::credential::Boundary,
        credential_id: String,
        slot: usize,
    },
    CredentialList {
        boundary: crate::credential::Boundary,
        slot: usize,
    },
    CredentialExists {
        boundary: crate::credential::Boundary,
        slot: usize,
    },
    CredentialAuthenticate {
        secret_hash: String,
        now: f64,
        slot: usize,
    },
    CredentialValidate {
        secret_hash: String,
        now: f64,
        slot: usize,
    },
    CredentialIdentify {
        secret_hash: String,
        slot: usize,
    },
    CredentialTouch {
        boundary: crate::credential::Boundary,
        credential_id: String,
        used_at: f64,
        touch_if_before: f64,
        slot: usize,
    },
    CredentialUpdate {
        boundary: crate::credential::Boundary,
        credential_id: String,
        request: crate::credential::UpdateRequest,
        slot: usize,
    },
    CredentialRevoke {
        boundary: crate::credential::Boundary,
        credential_id: String,
        revoked_at: f64,
        slot: usize,
    },
    AutomationSkillSourceUpdate {
        request: crate::automation_skill_source::Request,
        slot: usize,
    },
    AutomationProjectUpdate {
        request: crate::automation_project::Request,
        slot: usize,
    },
    AutomationDefinitionUpdate {
        request: crate::automation_definition::Request,
        slot: usize,
    },
    AutomationEventSettle {
        request: crate::automation_events::Request,
        slot: usize,
    },
    AutomationDefinitionDraftGuard {
        key: String,
    },
    AutomationRunMutation {
        request: crate::automation_run::Request,
        advance: bool,
        slot: usize,
    },
    AutomationRunCancel {
        request: crate::automation_run::CancelRequest,
        slot: usize,
    },
    AutomationRunApprove {
        request: crate::automation_approval::ApproveRequest,
        slot: usize,
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
        slot: usize,
    },
    WorkerJobGet {
        task_id: String,
        user_id: u64,
        slot: usize,
    },
    WorkerJobClaimNext {
        worker_id: String,
        now_ms: u64,
        lease_ms: u64,
        task_kinds: Vec<String>,
        slot: usize,
    },
    WorkerJobDefer {
        task_id: String,
        worker_id: String,
        fencing_token: u64,
        now_ms: u64,
        available_at_ms: u64,
        replay_cursor: u64,
        observed_cancel_sequence: u64,
        observed_wake_sequence: u64,
        slot: usize,
    },
    WorkerJobHeartbeat {
        task_id: String,
        worker_id: String,
        fencing_token: u64,
        now_ms: u64,
        lease_ms: u64,
        replay_cursor: u64,
        slot: usize,
    },
    WorkerJobClaimState {
        task_id: String,
        worker_id: String,
        fencing_token: u64,
        now_ms: u64,
        slot: usize,
    },
    WorkerJobRequestCancel {
        task_id: String,
        user_id: u64,
        now_ms: u64,
        reason: String,
        slot: usize,
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
        slot: usize,
    },
    TurnAppendSettled {
        conversation_id: String,
        actor: String,
        status: String,
        projection_json: Vec<u8>,
        projection_timestamp: Option<serde_json::Value>,
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
        slot: usize,
    },
    TurnCreatePair {
        request: Box<crate::turn::CreatePairRequest>,
        slot: usize,
    },
    TurnQueueActivate {
        request: crate::turn::QueueTransitionRequest,
        slot: usize,
    },
    TurnQueueCancel {
        request: crate::turn::QueueTransitionRequest,
        slot: usize,
    },
    TurnSteerCommit {
        request: crate::turn::SteerCommitRequest,
        slot: usize,
    },
    TurnRelatedAnnounce {
        request: crate::turn::RelatedAnnounceRequest,
        slot: usize,
    },
    TurnVisibleSync {
        request: Box<crate::turn::VisibleSyncRequest>,
        slot: usize,
    },
    TurnExists {
        conversation_id: String,
        slot: usize,
    },
    TurnGet {
        conversation_id: String,
        turn_id: String,
        slot: usize,
    },
    TurnImageGet {
        conversation_id: String,
        turn_id: String,
        projection_revision: u64,
        image_index: usize,
        slot: usize,
    },
    TurnContextRead {
        conversation_id: String,
        turn_id: String,
        replay: bool,
        after_turn_id: Option<String>,
        slot: usize,
    },
    TurnList {
        conversation_id: String,
        lane_id: Option<String>,
        slot: usize,
    },
    TurnListDelta {
        conversation_id: String,
        since_ms: u64,
        known_revisions: std::collections::BTreeMap<String, u64>,
        server_now_ms: u64,
        slot: usize,
    },
    TurnRevision {
        conversation_id: String,
        slot: usize,
    },
    TurnTimingTraceGet {
        task_id: String,
        slot: usize,
    },
    TurnTimingTraceList {
        conversation_id: String,
        before_created_at: Option<u64>,
        limit: usize,
        slot: usize,
    },
    TurnPerceptionRecord {
        request: crate::turn::PerceptionRecordRequest,
        slot: usize,
    },
    TurnRecover {
        request: crate::turn::RecoverRequest,
        slot: usize,
    },
    TurnEventRecord {
        request: crate::turn::EventRecordRequest,
        slot: usize,
    },
    TurnAttemptCreate {
        request: crate::turn::AttemptCreateRequest,
        slot: usize,
    },
    TurnAttemptDispatchableList {
        created_before_ms: u64,
        limit: usize,
        slot: usize,
    },
    TurnAttemptDispatchWorker {
        request: crate::turn::AttemptDispatchWorkerRequest,
        slot: usize,
    },
    TurnAttemptGet {
        attempt_id: String,
        include_execution_config: bool,
        slot: usize,
    },
    TurnAttemptClaim {
        attempt_id: String,
        dispatch_owner_id: String,
        committed_at_ms: u64,
        slot: usize,
    },
    TurnAttemptBind {
        attempt_id: String,
        task_id: String,
        dispatch_owner_id: String,
        committed_at_ms: u64,
        slot: usize,
    },
    TurnAttemptStart {
        attempt_id: String,
        task_id: String,
        automation_context: Option<crate::automation_events::TaskStartedContext>,
        committed_at_ms: u64,
        slot: usize,
    },
    TurnEventsList {
        attempt_id: String,
        after: u64,
        limit: usize,
        patch_mode: bool,
        slot: usize,
    },
    TurnEventsPrune {
        request: crate::turn::EventPruneRequest,
        slot: usize,
    },
    TurnDelete {
        conversation_id: String,
        turn_ids: Vec<String>,
        deleted_at_ms: u64,
        slot: usize,
    },
    TurnCompact {
        request: crate::turn::CompactRequest,
        slot: usize,
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
        slot: usize,
    },
    TurnBranchDelete {
        conversation_id: String,
        parent_turn_id: String,
        lane_id: String,
        deleted_at_ms: u64,
        committed_at_ms: u64,
        slot: usize,
    },
    TurnProjectionUpdate {
        preserve_activity: bool,
        human_guidance_answer_json: Option<Vec<u8>>,
        conversation_id: String,
        turn_id: String,
        projection_json: Vec<u8>,
        expected_projection_revision: u64,
        updated_at_ms: u64,
        committed_at_ms: u64,
        slot: usize,
    },
    TurnSyncSnapshot {
        conversation_id: String,
        turn_limit: usize,
        max_projection_bytes: usize,
        include_artifact_hint: bool,
        slot: usize,
    },
    TurnSyncRoundDirectory {
        conversation_id: String, lane_id: String, offset: u64, limit: usize,
        sync_sequence: u64, anchor_turn_id: String, slot: usize,
    },
    TurnSyncPage {
        conversation_id: String,
        lane_id: String,
        before_ordinal: u64,
        limit: usize,
        sync_sequence: u64,
        max_projection_bytes: usize,
        slot: usize,
    },
    TurnSyncChanges {
        conversation_id: String,
        after: u64,
        limit: usize,
        slot: usize,
    },
    TurnSyncPrune {
        request: crate::turn::SyncPruneRequest,
        slot: usize,
    },
    TurnCleanup {
        request: crate::turn::CleanupRequest,
        slot: usize,
    },
    TurnSearchBackfillGlobal {
        request: crate::semantic_executor::search_backfill_global::GlobalBackfillRequest,
        slot: usize,
    },
    EntityRead {
        key: EntityKey,
        slot: usize,
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
        slot: usize,
    },
    VersionedDocumentList {

        include_value: bool,
        start: EntityKey,
        end: EntityKey,
        namespace: String,
        limit: usize,
        reverse: bool,
        slot: usize,
    },
    VersionedDocumentPut {
        key: EntityKey,
        namespace: String,
        logical_key: String,
        value_json: Vec<u8>,
        expected_version: Option<u64>,
        updated_at_ms: u64,
        slot: usize,
    },
    VersionedDocumentDelete {
        key: EntityKey,
        namespace: String,
        logical_key: String,
        expected_version: Option<u64>,
        slot: usize,
    },
    RecentProjectList {
        slot: usize,
    },
    RecentProjectTouch {
        path: String,
        last_used: u64,
        updated_at_ms: u64,
        slot: usize,
    },
    RecentProjectTouchMany {
        paths: Vec<String>,
        last_used: u64,
        updated_at_ms: u64,
        slot: usize,
    },
    RecentProjectClear {
        slot: usize,
    },
    ProjectRelink {
        old_path: String,
        new_path: String,
        updated_at_ms: u64,
        slot: usize,
    },
    Queue {
        request: crate::queue::Request,
        slot: usize,
    },
    ModelContinuation {
        request: crate::model_continuation::Request,
        slot: usize,
    },
    Invocation {
        request: crate::invocation::Request,
        slot: usize,
    },
    Orchestration {
        request: crate::orchestration::Request,
        slot: usize,
    },
    IntegrationWorkspace {
        request: crate::integration_workspace::Request,
        slot: usize,
    },
    Swarm {
        request: crate::swarm::Request,
        slot: usize,
    },
    Scheduler {
        request: crate::scheduler::Request,
        slot: usize,
    },
    Timer {
        request: crate::timer::Request,
        slot: usize,
    },
    DailyCostMonth {
        year: u64,
        month: u64,
        slot: usize,
    },
    DailyCostLatest {
        slot: usize,
    },
    DailyCostPersistedDates {
        dates: Vec<String>,
        slot: usize,
    },
    DailyCostUpsert {
        date: String,
        cost: f64,
        conversations_json: Vec<u8>,
        computed_at: u64,
        updated_at_ms: u64,
        slot: usize,
    },
    DailyCostDelete {
        date: Option<String>,
        slot: usize,
    },
    LogAggregateFlush {
        rows_json: Vec<u8>,
        cutoff_ms: Option<u64>,
        updated_at_ms: u64,
        slot: usize,
    },
    LogAggregateQuery {
        level: String,
        q: String,
        sort: crate::log_aggregate::QuerySort,
        limit: usize,
        slot: usize,
    },
    PluginRegister {
        manifest_json: Vec<u8>,
        updated_at_ms: u64,
        slot: usize,
    },
    PluginManifestGet {
        namespace: String,
        slot: usize,
    },
    PluginRowOperation {
        request: crate::plugin_rows::DynRequest,
        slot: usize,
    },
    Paper {
        request: crate::paper::Request,
        slot: usize,
    },
    PaperBuildArtifact {
        request: crate::paper_build_artifact::Request,
        slot: usize,
    },
    PaperDocument {
        request: crate::paper_document::Request,
        slot: usize,
    },
    PaperArtifact {
        request: crate::paper_artifact::Request,
        slot: usize,
    },
    PaperLibrary {
        request: crate::paper_library::Request,
        slot: usize,
    },
    PaperPodcast {
        request: crate::paper_podcast::Request,
        slot: usize,
    },
    RawArchive {
        request: crate::raw_archive::Request,
        slot: usize,
    },
    Research {
        request: crate::research::ResearchRequest,
        slot: usize,
    },
    Optimizer {
        request: crate::optimizer::Request,
        slot: usize,
    },
    Knowledge {
        request: crate::knowledge::KnowledgeRequest,
        slot: usize,
    },
    ProjectBrainActiveList {
        slot: usize,
    },
    ProjectBrainRecoverySnapshot {
        cursor: Option<crate::project_brain::RecoveryCursor>,
        slot: usize,
    },
    ProjectBrainGet {
        project_key: String,
        slot: usize,
    },
    ProjectBrainCommand {
        project_key: String,
        action: crate::project_brain::CommandAction,
        payload_json: Vec<u8>,
        timestamp: u64,
        slot: usize,
    },
    ProjectBrainRebuild {
        project_key: String,
        timestamp: u64,
        slot: usize,
    },
    BrowserSiteObservationGet {
        origin: String,
        route_family: String,
        operation: String,
        now_ms: u64,
        slot: usize,
    },
    BrowserSiteObservationRecord {
        origin: String,
        route_family: String,
        operation: String,
        outcome: String,
        observed_at_ms: u64,
        observation_json: Option<Vec<u8>>,
        committed_at_ms: u64,
        slot: usize,
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
        require_attempts_live: Vec<String>,
        slot: usize,
    },
    IndexedStreamAppendBatch {
        items: Vec<IndexedStreamAppendItem>,
        slot: usize,
    },
    IndexedStreamList {
        task_id: String,
        after_sequence: Option<u64>,
        limit: usize,
        types: Vec<String>,
        type_prefixes: Vec<String>,
        payload_types: Option<Vec<String>>,
        slot: usize,
    },
    IndexedStreamInspectorSummary {
        root_task_ids: Vec<String>,
        slot: usize,
    },
    IndexedStreamPrune {
        created_before_ms: i64,
        limit: usize,
        retention_class: IndexedStreamRetentionClass,
        slot: usize,
    },
    IndexedStreamBounds {
        task_id: String,
        include_read_guard: bool,
        types: Option<Vec<String>>,
        slot: usize,
    },
    IndexedStreamLatest {
        task_id: String,
        metadata_only: bool,
        slot: usize,
    },
}

#[derive(Clone, Debug)]
pub enum TransactionIrResult {
    Literal(Vec<u8>),
    SlotOrLiteral { slot: usize, missing: Vec<u8> },
}

#[derive(Clone, Debug)]
pub struct TransactionIrCommandEffects {
    /// The exact hashed request payload explicitly names the authenticated owner.
    pub legacy_receipt_owner_bound: bool,
    pub command_id: String,
    pub request_digest: [u8; 32],
    /// Digest of the same canonical request with the session-injected
    /// `user_id` removed. Set only when the received payload explicitly
    /// carried the authenticated owner. Receipts written by the legacy v1
    /// authority for callers that omitted `user_id` (its receipt digest
    /// covered the payload exactly as sent) must replay identically after
    /// migration instead of conflicting on the injected field; ownership is
    /// already proven by the (tenant, owner, command_id) receipt key.
    pub alternate_request_digest: Option<[u8; 32]>,
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
    pub operation: String,
    pub operation_kind: StorageOperationKind,
    pub steps: Vec<TransactionIrStep>,
    pub result: TransactionIrResult,
    pub command_effects: Option<TransactionIrCommandEffects>,
}

fn invalid_input(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, message)
}

fn validate_provider_step_slot(
    initialized_slots: &mut SparseSlotValues<bool>,
    slot: usize,
    tenant_label: &str,
    provider_id: Option<&str>,
) -> io::Result<()> {
    let initialized = initialized_slots
        .get_or_insert(slot as usize);
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
    initialized_slots: &mut SparseSlotValues<bool>,
    slot: usize,
    tenant_label: &str,
) -> io::Result<()> {
    let initialized = initialized_slots
        .get_or_insert(slot as usize);
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
    serde_json::from_slice::<serde_json::Value>(bytes)
            .ok()
            .is_some_and(|value| value.is_object())
}

fn validate_worker_job_slot(
    initialized_slots: &mut SparseSlotValues<bool>,
    slot: usize,
) -> io::Result<()> {
    let initialized = initialized_slots
        .get_or_insert(slot as usize);
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
    crate::generated_tofudb_ir::BILLING_LEDGER_KINDS.contains(&value)
}

fn literal_bytes(result: &TransactionIrResult) -> usize {
    match result {
        TransactionIrResult::Literal(value) => value.len(),
        TransactionIrResult::SlotOrLiteral { missing, .. } => missing.len(),
    }
}

fn referenced_slot(condition: EntityWriteCondition) -> Option<usize> {
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
        {
            return Err(invalid_input("invalid Transaction IR envelope"));
        }
        let mut initialized_slots = SparseSlotValues::<bool>::new();
        let mut literal_bytes = literal_bytes(&self.result);
        let mut task_result_bytes = 0_usize;
        let mut swarm_document_bytes = 0_usize;
        let mut archive_document_bytes = 0_usize;
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
                        .get_or_insert(*slot as usize);
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
                            .is_some_and(|value| !(1..=MAX_TASK_RESULT_CACHE_FACT).contains(&value))
                        || last_turn_cache_read
                            .is_some_and(|value| value > MAX_TASK_RESULT_CACHE_FACT)
                        || !serde_json::from_slice::<serde_json::Value>(value_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid task result checkpoint step"));
                    }
                }
                TransactionIrStep::TaskResultRecordGet {
                    task_id,
                    requested_user_id,
                    ..
                }
                | TransactionIrStep::TaskResultReplayGet {
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
                        TransactionIrStep::TaskResultRecordGet { slot, .. }
                        | TransactionIrStep::TaskResultReplayGet { slot, .. }
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
                TransactionIrStep::TaskResultRecoveryPending { slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
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
                    archive_scope,
                    messages_json,
                    summary,
                    receipt_json,
                    trigger,
                    task_id,
                    model,
                    reason,
                    created_at_ms,
                    committed_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if archive_id.is_empty()
                        || archive_id.chars().count() > 128
                        || conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || archive_scope.chars().count() > 1024
                        || *created_at_ms >= crate::compaction_archive::MAX_ARCHIVE_CREATED_AT_MS
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
                    archive_document_bytes = archive_document_bytes
                        .checked_add(messages_json.len())
                        .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES)
                        .ok_or_else(|| invalid_input("compaction archive transaction exceeds its body bound"))?;
                    literal_bytes = [
                        archive_id.len(),
                        conversation_id.len(),
                        archive_scope.len(),
                        summary.len(),
                        receipt_json.len(),
                        trigger.len(),
                        task_id.len(),
                        model.len(),
                        reason.len(),
                    ]
                    .into_iter()
                    .try_fold(literal_bytes, |bytes, value| bytes.checked_add(value))
                    .ok_or_else(|| invalid_input("Transaction IR literal byte count overflow"))?;
                }
                TransactionIrStep::CompactionArchiveUpdateSummary {
                    archive_id,
                    archive_scope,
                    summary,
                    receipt_json,
                    post_messages_json,
                    committed_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if archive_id.is_empty()
                        || archive_id.chars().count() > 128
                        || archive_scope.chars().count() > 1024
                        || summary.chars().count() > 200_000
                        || *committed_at_ms == 0
                        || receipt_json.as_ref().is_some_and(|value| {
                            value.len() > 32 * 1024
                                || !serde_json::from_slice::<serde_json::Value>(value)
                                    .is_ok_and(|value| value.is_object())
                        })
                        || post_messages_json.as_ref().is_some_and(|value| {
                            !serde_json::from_slice::<serde_json::Value>(value).is_ok_and(
                                |document| {
                                    document.as_array().is_some_and(|items| {
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
                        })
                    {
                        return Err(invalid_input("invalid compaction archive summary step"));
                    }
                    archive_document_bytes = archive_document_bytes
                        .checked_add(post_messages_json.as_ref().map_or(0, Vec::len))
                        .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES)
                        .ok_or_else(|| invalid_input("compaction archive transaction exceeds its body bound"))?;
                    literal_bytes = literal_bytes
                        .checked_add(archive_id.len())
                        .and_then(|bytes| bytes.checked_add(summary.len()))
                        .and_then(|bytes| {
                            bytes.checked_add(receipt_json.as_ref().map_or(0, Vec::len))
                        })
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(namespace.len())
                        .and_then(|bytes| bytes.checked_add(logical_key.len()))
                        .and_then(|bytes| bytes.checked_add(value_json.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ProjectRelink {
                    old_path,
                    new_path,
                    updated_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Queue { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ModelContinuation { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Invocation { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Orchestration { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::IntegrationWorkspace { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Swarm { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Blob-capable checkpoints have their own aggregate budget,
                    // like task results; do not charge their bodies to inline IR.
                    swarm_document_bytes = swarm_document_bytes
                        .checked_add(request.validate()?)
                        .filter(|bytes| *bytes <= crate::generated_tofudb_ir::MAX_SWARM_RESPONSE_BYTES)
                        .ok_or_else(|| {
                            invalid_input("swarm documents exceed their aggregate transaction bound")
                        })?;
                }
                TransactionIrStep::Scheduler { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate(self.owner_user_id)?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Timer { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate(self.owner_user_id)?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if !crate::daily_cost::valid_date(date)
                        || !cost.is_finite()
                        || !(0.0..=1_000_000_000.0).contains(cost)
                        || *updated_at_ms == 0
                        || !serde_json::from_slice::<serde_json::Value>(conversations_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid daily-cost upsert step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(date.len())
                        .and_then(|bytes| bytes.checked_add(conversations_json.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::DailyCostMonth { year, month, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if dates
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::DailyCostDelete { date, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::LogAggregateFlush {
                    rows_json,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PluginRegister {
                    manifest_json,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if serde_json::from_slice::<Value>(manifest_json).is_err() {
                        return Err(invalid_input("invalid plugin manifest step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(manifest_json.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PluginManifestGet { namespace, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PluginRowOperation { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Paper { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PaperLibrary { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PaperArtifact { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PaperBuildArtifact { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PaperDocument { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::PaperPodcast { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::RawArchive { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Compressed bodies are bounded blob inputs. Count only
                    // routing metadata against the inline IR literal budget.
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Research { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Report/workspace bodies are separately bounded blob
                    // inputs rather than inline Transaction IR literals.
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Optimizer { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    literal_bytes = literal_bytes
                        .checked_add(request.validate(self.owner_user_id)?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::Knowledge { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    // Document/chunk bodies are separately bounded blob
                    // inputs rather than inline Transaction IR literals.
                    literal_bytes = literal_bytes
                        .checked_add(request.validate()?)
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if crate::project_brain::normalize_project_key(project_key).as_deref()
                        != Some(project_key.as_str())
                        || *timestamp > i64::MAX as u64
                        || !serde_json::from_slice::<serde_json::Value>(payload_json)
                            .is_ok_and(|value| value.is_object())
                    {
                        return Err(invalid_input("invalid Project Brain command step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(project_key.len())
                        .and_then(|bytes| bytes.checked_add(payload_json.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ProjectBrainActiveList { slot }
                | TransactionIrStep::ProjectBrainRecoverySnapshot { slot, .. } => {
                    if let TransactionIrStep::ProjectBrainRecoverySnapshot {
                        cursor: Some(cursor),
                        ..
                    } = step
                    {
                        cursor.validate(self.owner_user_id)?;
                        literal_bytes = literal_bytes
                            .checked_add(cursor.project_key.len())
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
                            })?;
                    }
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ProjectBrainGet { project_key, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::RecentProjectTouchMany {
                    paths,
                    updated_at_ms,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if paths.is_empty()
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ConversationDelete {
                    conversation_id,
                    deleted_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ConversationRestore {
                    conversation_id,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ConversationPurge {
                    conversation_id,
                    purged_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ConversationTrashPrune {
                    deleted_before_ms,
                    maximum_conversations,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                TransactionIrStep::ConversationDiagnostic { request, slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    request.validate()?;
                    literal_bytes = literal_bytes
                        .checked_add(request.conversation_id.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ConversationGet {
                    conversation_id,
                    include_messages,
                    message_window,
                    before_sequence,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::EntityPut {
                    value, condition, ..
                } => {
                    literal_bytes = literal_bytes
                        .checked_add(value.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::IndexedStreamAppend {
                    task_id,
                    payload_json,
                    created_at_ms,
                    require_attempts_live,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if task_id.is_empty() || *created_at_ms <= 0 {
                        return Err(invalid_input("invalid indexed stream append"));
                    }
                    validate_attempt_fence(require_attempts_live)?;
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|bytes| bytes.checked_add(payload_json.len()))
                        .and_then(|bytes| {
                            require_attempts_live
                                .iter()
                                .try_fold(bytes, |total, id| total.checked_add(id.len()))
                        })
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::IndexedStreamAppendBatch { items, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if items.is_empty() {
                        return Err(invalid_input("Transaction IR event batch is invalid"));
                    }
                    for item in items {
                        if item.task_id.is_empty() || item.created_at_ms <= 0 {
                            return Err(invalid_input("invalid indexed stream batch item"));
                        }
                        validate_attempt_fence(&item.require_attempts_live)?;
                        literal_bytes = literal_bytes
                            .checked_add(item.task_id.len())
                            .and_then(|bytes| bytes.checked_add(item.payload_json.len()))
                            .and_then(|bytes| {
                                item.require_attempts_live
                                    .iter()
                                    .try_fold(bytes, |total, id| total.checked_add(id.len()))
                            })
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ConversationActivityDates {
                    day_boundaries_ms,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ModelRoutingGet { tenant_label, slot }
                | TransactionIrStep::ModelRoutingMigrationReceiptGet { tenant_label, slot }
                | TransactionIrStep::ModelRoutingSecretList { tenant_label, slot } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                    // The owner-bound blob codec owns durable document bodies.
                    // The shared literal budget still applies to IR metadata.
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .ok_or_else(|| invalid_input("Transaction IR metadata exceeds its bound"))?;
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
                    // The owner-bound blob codec owns durable document bodies.
                    // The shared literal budget still applies to IR metadata.
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .ok_or_else(|| invalid_input("Transaction IR metadata exceeds its bound"))?;
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ModelRoutingSecretPrune {
                    tenant_label,
                    active_secret_references,
                    updated_before,
                    slot,
                } => {
                    validate_model_routing_slot(&mut initialized_slots, *slot, tenant_label)?;
                    if active_secret_references.iter().any(|reference| {
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::ProviderList { tenant_label, slot } => {
                    validate_provider_step_slot(&mut initialized_slots, *slot, tenant_label, None)?;
                    literal_bytes = literal_bytes
                        .checked_add(tenant_label.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::AutomationSkillSourceUpdate {request,slot} => {
                    validate_worker_job_slot(&mut initialized_slots,*slot)?;
                    request.validate(self.owner_user_id,u64::MAX)?;
                    literal_bytes=literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(||invalid_input("skill source literal bytes exceed bound"))?;
                }
                TransactionIrStep::AutomationProjectUpdate {request,slot} => {
                    validate_worker_job_slot(&mut initialized_slots,*slot)?;
                    request.validate(self.owner_user_id,u64::MAX)?;
                    literal_bytes=literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(||invalid_input("project registration literal bytes exceed bound"))?;
                }
                TransactionIrStep::AutomationDefinitionUpdate {request,slot} => {
                    validate_worker_job_slot(&mut initialized_slots,*slot)?;
                    request.validate(self.owner_user_id,u64::MAX)?;
                    literal_bytes=literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(||invalid_input("publication literal bytes exceed bound"))?;
                }
                TransactionIrStep::AutomationEventSettle { request, slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    request.validate(self.owner_user_id, u64::MAX)?;
                    literal_bytes = literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(|| invalid_input("event settlement literal bytes exceed bound"))?;
                }
                TransactionIrStep::AutomationDefinitionDraftGuard {key} => {
                    if key.is_empty() || key.chars().count()>512 { return Err(invalid_input("invalid draft key")); }
                    literal_bytes=literal_bytes.checked_add(key.len())
                        .ok_or_else(||invalid_input("draft guard literal bytes exceed bound"))?;
                }
                TransactionIrStep::AutomationRunCancel { request, slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    request.validate(self.owner_user_id, u64::MAX)?;
                    literal_bytes = literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(|| invalid_input("automation cancellation literal bytes exceed bound"))?;
                }
                TransactionIrStep::AutomationRunApprove { request, slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    request.validate(self.owner_user_id, u64::MAX)?;
                    literal_bytes = literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(|| invalid_input("automation approval literal bytes exceed bound"))?;
                }
            TransactionIrStep::AutomationRunMutation { request, advance, slot } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    request.validate(*advance, self.owner_user_id, u64::MAX)?;
                    literal_bytes = literal_bytes.checked_add(request.literal_bytes()?)
                        .ok_or_else(|| invalid_input("automation literal bytes exceed bound"))?;
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
                    if self.operation=="worker_job.enqueue" && crate::generated_tofudb_ir::AUTOMATION_RESERVED_WORKER_TASK_KINDS.contains(&task_kind.as_str()) {
                        return Err(invalid_input("automation jobs require their atomic owner"));
                    }
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::WorkerJobDefer {
                    task_id,
                    worker_id,
                    fencing_token,
                    now_ms,
                    available_at_ms,
                    slot,
                    ..
                } => {
                    validate_worker_job_slot(&mut initialized_slots, *slot)?;
                    if !valid_worker_job_text(task_id,256,false) || !valid_worker_job_text(worker_id,256,false)
                        || *fencing_token==0 || *now_ms>MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || *available_at_ms < *now_ms || *available_at_ms > MAX_WORKER_JOB_CLOCK_MILLISECONDS
                        || available_at_ms.saturating_sub(*now_ms)>crate::generated_tofudb_ir::MAX_WORKER_JOB_FUTURE_CLOCK_SKEW_MILLISECONDS {
                        return Err(invalid_input("invalid worker suspension step"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .and_then(|b| b.checked_add(worker_id.len()))
                        .ok_or_else(|| {
                            invalid_input("worker suspension literal bytes exceed bound")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnCreatePair { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnQueueActivate { request, slot }
                | TransactionIrStep::TurnQueueCancel { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnSteerCommit { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnRelatedAnnounce { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                }
                TransactionIrStep::TurnVisibleSync { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                            .ok_or_else(|| {
                                invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty() || conversation_id.chars().count() > 256 {
                        return Err(invalid_input("invalid Turn conversation identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                }
                | TransactionIrStep::TurnContextRead {
                    conversation_id,
                    turn_id,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnTimingTraceGet { task_id, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if task_id.is_empty() || task_id.chars().count() > 256 {
                        return Err(invalid_input("invalid timing trace task identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(task_id.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnTimingTraceList {
                    conversation_id,
                    before_created_at,
                    limit,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnPerceptionRecord { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnRecover { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                }
                TransactionIrStep::TurnEventRecord { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        {
                            return Err(invalid_input("invalid carried Turn task event"));
                        }
                    }
                    if !request.preceding_task_events.is_empty() {
                        let last = request.task_event.as_ref()
                            .ok_or_else(|| invalid_input("progress event batch needs a final carrier"))?;
                        if request.terminal || request.preceding_task_events.len() >= crate::generated_task_event_delivery::PROGRESS_BATCH_MAX_FRAMES
                            || last.task_id != request.task_id {
                            return Err(invalid_input("invalid progress event batch scope"));
                        }
                        let wire_bytes = request.preceding_task_events.iter().chain(std::iter::once(last))
                            .try_fold(0usize, |total, item| total.checked_add(item.payload_json.len())
                                .ok_or_else(|| invalid_input("progress event batch wire byte overflow")))?;
                        if wire_bytes > crate::generated_task_event_delivery::PROGRESS_BATCH_MAX_WIRE_BYTES {
                            return Err(invalid_input("progress event batch exceeds wire budget"));
                        }
                        let mut expected = last.application_sequence.checked_sub(request.preceding_task_events.len() as u64)
                            .ok_or_else(|| invalid_input("invalid progress event batch sequence"))?;
                        for item in request.preceding_task_events.iter().chain(std::iter::once(last)) {
                            if item.task_id != last.task_id || item.application_sequence != expected
                                || !matches!(item.event_type.as_str(), "delta" | "program_output" | "tool_progress") {
                                return Err(invalid_input("invalid progress event batch scope/order"));
                            }
                            expected = expected.checked_add(1)
                                .ok_or_else(|| invalid_input("progress event batch sequence overflow"))?;
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
                    let preceding_bytes = request.preceding_task_events.iter().try_fold(0usize, |total, item| {
                        total.checked_add(item.payload_json.len())
                            .and_then(|bytes| bytes.checked_add(item.task_id.len()))
                            .and_then(|bytes| bytes.checked_add(item.event_type.len()))
                            .ok_or_else(|| invalid_input("progress event batch byte count overflow"))
                    })?;
                    literal_bytes = literal_bytes
                        .checked_add(preceding_bytes)
                        .and_then(|bytes| bytes.checked_add(request.attempt_id.len()))
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnList {
                    conversation_id,
                    lane_id,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                }
                TransactionIrStep::TurnAttemptCreate { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        || (request.operation == "regenerate"
                            && request.input_update.is_some()
                                != request.expected_input_projection_revision.is_some())
                        || (request.operation != "regenerate"
                            && request.input_update.as_ref().is_some_and(|update| {
                                !crate::turn::valid_resume_input_update(update)
                            }))
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
                    .ok_or_else(|| invalid_input("Transaction IR literal byte count overflow"))?;
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnAttemptGet { attempt_id, slot, .. } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if attempt_id.is_empty() || attempt_id.chars().count() > 128 {
                        return Err(invalid_input("invalid Turn attempt identity"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(attempt_id.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnAttemptClaim {
                    attempt_id,
                    dispatch_owner_id,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnAttemptStart {
                    attempt_id,
                    task_id,
                    automation_context,
                    committed_at_ms,
                    slot,
                } => {
                    if let Some(context) = automation_context { context.validate()?; }
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnEventsList {
                    attempt_id,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnEventsPrune { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .get_or_insert(*slot as usize);
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
                }
                TransactionIrStep::TurnCompact { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
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
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnProjectionUpdate {
                    preserve_activity: _,
                    human_guidance_answer_json,
                    conversation_id,
                    turn_id,
                    projection_json,
                    expected_projection_revision: _,
                    updated_at_ms,
                    committed_at_ms,
                    slot,
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        || human_guidance_answer_json.as_ref().is_some_and(|bytes| {
                            bytes.len() > crate::generated_tofudb_ir::MAX_HUMAN_GUIDANCE_PAYLOAD_BYTES || serde_json::from_slice::<serde_json::Value>(bytes)
                                .ok().is_none_or(|value| !value.is_object())
                        })
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
                        .and_then(|bytes| bytes.checked_add(human_guidance_answer_json.as_ref().map_or(0, Vec::len)))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnSyncSnapshot {
                    conversation_id,
                    turn_limit,
                    max_projection_bytes,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || *turn_limit > 256
                        || (*max_projection_bytes != 0 && !(65536..=16777216).contains(max_projection_bytes))
                    {
                        return Err(invalid_input("invalid Turn sync snapshot"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnSyncRoundDirectory { conversation_id, lane_id, anchor_turn_id, limit, slot, .. } => {
                    let initialized = initialized_slots.get_or_insert(*slot as usize);
                    if *initialized { return Err(invalid_input("Transaction IR initializes a slot twice")); }
                    *initialized = true;
                    if conversation_id.is_empty() || conversation_id.chars().count() > 256
                        || lane_id.is_empty() || lane_id.chars().count() > 128
                        || anchor_turn_id.chars().count() > 128 || !(1..=100).contains(limit) {
                        return Err(invalid_input("invalid Turn round directory"));
                    }
                    literal_bytes = literal_bytes.checked_add(conversation_id.len())
                        .and_then(|n| n.checked_add(lane_id.len()))
                        .and_then(|n| n.checked_add(anchor_turn_id.len()))
                        .ok_or_else(|| invalid_input("Transaction IR literal byte count overflow"))?;
                }
                TransactionIrStep::TurnSyncPage {
                    conversation_id,
                    lane_id,
                    limit,
                    max_projection_bytes,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || lane_id.is_empty()
                        || lane_id.chars().count() > 128
                        || !(1..=256).contains(limit)
                        || (*max_projection_bytes != 0 && !(65536..=16777216).contains(max_projection_bytes))
                    {
                        return Err(invalid_input("invalid Turn sync page"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(conversation_id.len())
                        .and_then(|bytes| bytes.checked_add(lane_id.len()))
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnSyncChanges {
                    conversation_id,
                    limit,
                    slot,
                    ..
                } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
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
                        .ok_or_else(|| {
                            invalid_input("Transaction IR literal byte count overflow")
                        })?;
                }
                TransactionIrStep::TurnSyncPrune { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.created_before_ms == 0 || !(1..=20_000).contains(&request.max_rows) {
                        return Err(invalid_input("invalid conversation sync prune"));
                    }
                }
                TransactionIrStep::TurnCleanup { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    if request.now_ms == 0
                        || !(1..=crate::generated_tofudb_ir::MAX_CLEANUP_ATTEMPTS_PER_TRANSACTION)
                            .contains(&request.limit)
                    {
                        return Err(invalid_input("invalid Turn cleanup"));
                    }
                }
                TransactionIrStep::TurnSearchBackfillGlobal { request, slot } => {
                    let initialized = initialized_slots
                        .get_or_insert(*slot as usize);
                    if *initialized {
                        return Err(invalid_input("Transaction IR initializes a slot twice"));
                    }
                    *initialized = true;
                    use crate::semantic_executor::search_backfill_global as global_backfill;
                    if !(1..=global_backfill::MAX_GLOBAL_BACKFILL_CLAIM_ROWS_PER_TRANSACTION)
                        .contains(&request.max_rows)
                        || !(1..=global_backfill::MAX_GLOBAL_BACKFILL_SOURCE_BYTES_PER_TRANSACTION)
                            .contains(&request.max_bytes)
                        || request.cursor.len() > global_backfill::MAX_GLOBAL_BACKFILL_CURSOR_BYTES
                    {
                        return Err(invalid_input("invalid global search backfill page"));
                    }
                    literal_bytes = literal_bytes
                        .checked_add(request.cursor.len())
                        .ok_or_else(|| invalid_input("Transaction IR literal byte count overflow"))?
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
                    offset,
                    selector,
                    limit,
                    ..
                } => {
                    crate::artifact::query::validate_input(offset, selector)?;
                    if artifact_ref.is_empty()
                        || artifact_ref.chars().count() > 96
                        || *now_ms == 0
                        || !(1..=crate::generated_tofudb_ir::MAX_TOOL_RESULT_RANGE_BYTES)
                            .contains(limit)
                    {
                        return Err(invalid_input("invalid tool-result read step"));
                    }
                    offset.to_string().len() + selector.len() + artifact_ref.len()
                }
                TransactionIrStep::ToolResultArtifactSearch {
                    artifact_ref,
                    query,
                    now_ms,
                    cursor,
                    selector,
                    limit,
                    ..
                } => {
                    crate::artifact::query::validate_input(cursor, selector)?;
                    if artifact_ref.is_empty()
                        || artifact_ref.chars().count() > 96
                        || query.is_empty()
                        || query.chars().count() > 200
                        || *now_ms == 0
                        || !(1..=20).contains(limit)
                    {
                        return Err(invalid_input("invalid tool-result search step"));
                    }
                    cursor.to_string().len() + selector.len() + artifact_ref.len() + query.len()
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
                    archive_scope,
                    limit,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || archive_scope.chars().count() > 1024
                        || !(1..=1_000).contains(limit)
                    {
                        return Err(invalid_input("invalid compaction archive list step"));
                    }
                    conversation_id.len() + archive_scope.len()
                }
                TransactionIrStep::CompactionArchiveGet {
                    conversation_id,
                    archive_id,
                    archive_scope,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || archive_id.is_empty()
                        || archive_id.chars().count() > 128
                        || archive_scope.chars().count() > 1024
                    {
                        return Err(invalid_input("invalid compaction archive get step"));
                    }
                    conversation_id.len() + archive_id.len() + archive_scope.len()
                }
                TransactionIrStep::CompactionArchiveDeleteConversation {
                    conversation_id,
                    archive_scope,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || archive_scope.chars().count() > 1024
                    {
                        return Err(invalid_input("invalid compaction archive delete step"));
                    }
                    conversation_id.len() + archive_scope.len()
                }
                TransactionIrStep::CompactionArchivePrune {
                    conversation_id,
                    archive_scope,
                    keep,
                    ..
                } => {
                    if conversation_id.is_empty()
                        || conversation_id.chars().count() > 256
                        || archive_scope.chars().count() > 1024
                        || !(1..=1_000).contains(keep)
                    {
                        return Err(invalid_input("invalid compaction archive prune step"));
                    }
                    conversation_id.len() + archive_scope.len()
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
                    payload_types,
                    ..
                } => {
                    if payload_types.as_ref().is_some_and(|values| {
                        values.len() > 64 || values.iter().any(|value| {
                            value.is_empty() || value.chars().count() > 128
                        })
                    }) {
                        return Err(invalid_input("Transaction IR event payload selection is unbounded"));
                    }
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
                .ok_or_else(|| invalid_input("Transaction IR literal byte count overflow"))?;
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
                        || matches!(step, TransactionIrStep::PaperBuildArtifact { request, .. } if request.mutates_state())
                        || matches!(step, TransactionIrStep::PaperDocument { request, .. } if request.mutates_state())
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
                            | TransactionIrStep::TurnCleanup { .. }
                            | TransactionIrStep::TurnDelete { .. }
                            | TransactionIrStep::TurnSearchBackfillGlobal { .. }
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
    slots: &SparseSlotValues<Option<Option<Vec<u8>>>>,
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

/// The bytes cannot be mutated while their parser/encoder certificate survives.
/// Raw/literal/replay results never acquire a certificate by operation name.
#[derive(Clone, Debug)]
pub(crate) struct IrResponse {
    bytes: Vec<u8>,
    json_proven: bool,
    receipt_cacheable_proven: bool,
    snapshot_stable: Option<crate::query_result_cache::Admission>,
    snapshot_read_complete: bool,
}
impl IrResponse {
    pub(crate) fn unverified(bytes: Vec<u8>) -> Self {
        Self {
            bytes,
            json_proven: false,
            receipt_cacheable_proven: false,
            snapshot_stable: None,
            snapshot_read_complete: false,
        }
    }
    pub(crate) fn json_proven(&self) -> bool {
        self.json_proven
    }
    pub(crate) fn snapshot_stable(&self) -> bool { self.snapshot_stable.is_some() }
    pub(crate) fn cache_admission(&self) -> Option<crate::query_result_cache::Admission> { self.snapshot_stable }
    pub(crate) fn certify_snapshot_stable(&mut self, ir: &TransactionIr) {
        // compile_records binds this operation's key only from request scope,
        // namespace and key. A pure storage step alone cannot certify a future
        // compiler that selects its document from a clock or another live input.
        let deterministic_record = ir.operation == "record.get"
            && matches!((&ir.steps[..], &ir.result),
                ([TransactionIrStep::VersionedDocumentGet { slot, .. }],
                 TransactionIrResult::SlotOrLiteral { slot: result, missing })
                    if slot == result && missing == b"null");
        // Snapshot fields come from the same entity root, including the
        // execution epoch, attempts and phase head. A deadline-shortened window
        // is request-dependent and must never become a reusable response.
        let complete_snapshot = self.snapshot_read_complete
            && ir.operation == "turn.sync.snapshot"
            && matches!((&ir.steps[..], &ir.result),
                ([TransactionIrStep::TurnSyncSnapshot { slot, .. }],
                 TransactionIrResult::SlotOrLiteral { slot: result, missing })
                    if slot == result && missing == b"null");
        self.snapshot_stable = ((deterministic_record || complete_snapshot)
            && ir.operation_kind == crate::generated_storage_operations::StorageOperationKind::Query
            && ir.command_effects.is_none()).then_some(if complete_snapshot {
                crate::query_result_cache::Admission::RepeatedSnapshot
            } else { crate::query_result_cache::Admission::RepeatedKey });
        // The selected absence literal is a complete canonical scalar. This
        // proves only these exact bytes, never a general unverified response.
        if self.snapshot_stable.is_some() && self.bytes == b"null" { self.json_proven = true; }
    }
    // Only the private dispatch certificate reconstructs this immutable proof.
    pub(crate) fn from_snapshot_cache(bytes: Vec<u8>, admission: crate::query_result_cache::Admission) -> Self {
        Self { bytes, json_proven: true, receipt_cacheable_proven: false, snapshot_stable: Some(admission), snapshot_read_complete: false }
    }


    fn from_encoded(response: crate::response_json::EncodedResponse) -> Self {
        let json_proven = response.json_proven();
        Self { bytes: response.into_bytes(), json_proven, receipt_cacheable_proven: false, snapshot_stable: None, snapshot_read_complete: false }
    }
    pub(crate) fn into_bytes(self) -> Vec<u8> {
        self.bytes
    }
}
impl std::ops::Deref for IrResponse {
    type Target = Vec<u8>;
    fn deref(&self) -> &Self::Target {
        &self.bytes
    }
}

/// Every mutable slot access invalidates its certificate before exposing bytes.
/// Sparse cells preserve condition/dataflow behavior without reserving unused slots.
struct IrSlots {
    bytes: SparseSlotValues<Option<Option<Vec<u8>>>>,
    json_proven: SparseSlotValues<bool>,
    receipt_cacheable_proven: SparseSlotValues<bool>,
}
impl IrSlots {
    fn new() -> Self {
        Self {
            bytes: SparseSlotValues::new(),
            json_proven: SparseSlotValues::new(),
            receipt_cacheable_proven: SparseSlotValues::new(),
        }
    }
    fn set_projection(
        &mut self,
        slot: usize,
        projection: Option<crate::versioned_document::DocumentProjection>,
    ) {
        self.set_response(slot, projection.map(|value| {
            let json_proven = value.json_proven();
            IrResponse { bytes: value.into_bytes(), json_proven, receipt_cacheable_proven: false, snapshot_stable: None, snapshot_read_complete: false }
        }));
    }

    fn set_response(&mut self, slot: usize, response: Option<IrResponse>) {
        self.receipt_cacheable_proven[slot] = false;
        self.json_proven[slot] = response.as_ref().is_some_and(|value| value.json_proven());
        self.bytes[slot] = Some(response.map(IrResponse::into_bytes));
    }
}
impl IrSlots {
    fn set_settled_append_response(&mut self, slot: usize, response: crate::turn::SettledAppendResponse) {
        self.bytes[slot] = Some(Some(response.into_bytes()));
        self.json_proven[slot] = false;
        self.receipt_cacheable_proven[slot] = true;
    }
}
impl std::ops::Deref for IrSlots {
    type Target = SparseSlotValues<Option<Option<Vec<u8>>>>;
    fn deref(&self) -> &Self::Target {
        &self.bytes
    }
}
impl std::ops::Index<usize> for IrSlots {
    type Output = Option<Option<Vec<u8>>>;
    fn index(&self, index: usize) -> &Self::Output {
        &self.bytes[index]
    }
}
impl std::ops::IndexMut<usize> for IrSlots {
    fn index_mut(&mut self, index: usize) -> &mut Self::Output {
        self.json_proven[index] = false;
        self.receipt_cacheable_proven[index] = false;
        &mut self.bytes[index]
    }
}

/// Final extraction transfers the selected immutable allocation after every IR
/// step has finished. The certificate follows those exact bytes, and the slot
/// is invalidated before its allocation leaves the execution frame.
fn result_bytes(result: &TransactionIrResult, slots: &mut IrSlots) -> io::Result<IrResponse> {
    let _extract = crate::fsync_stats::StageTimer::diagnostic(
        crate::fsync_stats::STAGE_IR_RESULT_EXTRACT);
    match result {
        TransactionIrResult::Literal(value) => {
            crate::request_profile::work(crate::request_profile::Work::IrResultCopiedBytes, value.len() as u64);
            Ok(IrResponse::unverified(value.clone()))
        }
        TransactionIrResult::SlotOrLiteral { slot, missing } => {
            let index = *slot as usize;
            let proven = slots.json_proven.get(index).copied().unwrap_or(false);
            let receipt_cacheable_proven = slots.receipt_cacheable_proven.get(index).copied().unwrap_or(false);
            let value = slots.bytes.get_mut(index).and_then(Option::take)
                .ok_or_else(|| invalid_input("Transaction IR result slot is unavailable"))?;
            slots.json_proven[index] = false;
            slots.receipt_cacheable_proven[index] = false;
            Ok(match value {
                Some(bytes) => {
                    crate::request_profile::work(crate::request_profile::Work::IrResultMovedBytes, bytes.len() as u64);
                    IrResponse { bytes, json_proven: proven, receipt_cacheable_proven, snapshot_stable: None, snapshot_read_complete: false }
                }
                None => {
                    crate::request_profile::work(crate::request_profile::Work::IrResultCopiedBytes, missing.len() as u64);
                    IrResponse::unverified(missing.clone())
                }
            })
        }
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
    if let Some(response) = crate::legacy_receipt::lookup(
        database,
        ir.tenant_id,
        ir.owner_user_id,
        &effects.command_id,
        &ir.operation,
        effects.request_digest,
        effects.legacy_receipt_owner_bound,
    )? {
        return Ok(Some(response));
    }
    if !effects.store_receipt {
        return Ok(None);
    }
    database.receipt_lookup_any(
        transaction,
        &effects.command_id,
        &ir.operation,
        effects.request_digest,
        effects.alternate_request_digest,
    )
}

pub fn execute_transaction_ir(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
) -> io::Result<Vec<u8>> {
    execute_transaction_ir_with_notices(database, ir, &mut Vec::new())
}

pub(crate) fn execute_transaction_ir_with_notices(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
    notices: &mut Vec<crate::authority::ConversationCommitNotice>,
) -> io::Result<Vec<u8>> {
    // Callers without a live request deadline (offline tooling, tests) get an
    // unbounded budget, preserving the pre-deadline-guard behavior exactly.
    execute_transaction_ir_with_deadline(database, ir, notices, Duration::MAX)
}

pub(crate) fn execute_transaction_ir_with_deadline(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
    notices: &mut Vec<crate::authority::ConversationCommitNotice>,
    deadline_budget: Duration,
) -> io::Result<Vec<u8>> {
    execute_transaction_ir_with_json_proof(database, ir, notices, deadline_budget).map(IrResponse::into_bytes)
}

pub(crate) fn execute_transaction_ir_with_json_proof(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
    notices: &mut Vec<crate::authority::ConversationCommitNotice>,
    deadline_budget: Duration,
) -> io::Result<IrResponse> {
    let prepared = prepare_transaction_ir_with_deadline(database, ir, deadline_budget)?;
    if let Some(transaction) = prepared.transaction {
        let _commit_timer =
            crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_IR_COMMIT);
        notices.extend(database.commit(transaction)?.conversation_notices);
    }
    Ok(prepared.response)
}

/// Logical result and uncommitted effects. The transaction owns its snapshot
/// and private blob buffers until validation/publication or cancellation.
pub(crate) struct PreparedTransactionIr {
    pub response: IrResponse,
    pub transaction: Option<AuthorityTransaction>,
}

pub(crate) fn prepare_transaction_ir_with_deadline(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
    deadline_budget: Duration,
) -> io::Result<PreparedTransactionIr> {
    prepare_transaction_ir_mode(database, ir, deadline_budget, IrStepExecution::Sequential)
}

/// Experimental explicit frame preparation. No production compiler or public
/// transport calls this port. The caller supplies one already-atomic IR; this
/// never joins requests, waits for future events or acknowledges before commit.
#[cfg(test)]
pub(crate) fn prepare_execution_frame_ir_with_deadline(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
    deadline_budget: Duration,
) -> io::Result<PreparedTransactionIr> {
    if !ir.operation_kind.mutates_state() {
        return Err(invalid_input("execution frame requires a command transaction"));
    }
    prepare_transaction_ir_mode(database, ir, deadline_budget, IrStepExecution::ExperimentalTurnFrame)
}

enum IrStepExecution {
    Sequential,
    #[cfg(test)]
    ExperimentalTurnFrame,
}

fn prepare_transaction_ir_mode(
    database: &mut AuthorityDatabase,
    ir: &TransactionIr,
    deadline_budget: Duration,
    execution: IrStepExecution,
) -> io::Result<PreparedTransactionIr> {
    // One wall-clock budget for the whole IR execution, shared by the long
    // read loops that materialize per-row payload documents (sync replay,
    // attempt event replay). They truncate to a resumable prefix instead of
    // holding the authority mutex past the caller's deadline.
    let started_at = Instant::now();
    let mut deadline_elapsed = || started_at.elapsed() >= deadline_budget;
    let mut snapshot_read_complete = false;
    ir.validate()?;
    let needs_identity_claim_scopes = ir.steps.iter().any(|step| {
        matches!(
            step,
            TransactionIrStep::PaperDocument { .. }
                | TransactionIrStep::PaperBuildArtifact { .. }
                | TransactionIrStep::RawArchive { .. }
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
                | TransactionIrStep::Invocation { .. }
                // Scoped archive list/get resolve the child birth record in
                // the tenant-global task-result namespace.
                | TransactionIrStep::CompactionArchiveList { .. }
                | TransactionIrStep::CompactionArchiveGet { .. }
                | TransactionIrStep::TurnEventRecord { .. }
                | TransactionIrStep::TurnAttemptClaim { .. }
                | TransactionIrStep::TurnAttemptBind { .. }
                | TransactionIrStep::TurnAttemptStart { .. }
                | TransactionIrStep::TurnEventsList { .. }
                | TransactionIrStep::TurnEventsPrune { .. }
                | TransactionIrStep::TurnSyncSnapshot { .. }
                | TransactionIrStep::TurnSyncRoundDirectory { .. }
                | TransactionIrStep::TurnSyncPage { .. }
                | TransactionIrStep::TurnSearchBackfillGlobal { .. }
                | TransactionIrStep::TurnCleanup { .. }
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
                | TransactionIrStep::AutomationEventSettle { .. }
                | TransactionIrStep::AutomationProjectUpdate { .. }
                | TransactionIrStep::AutomationSkillSourceUpdate { .. }
                | TransactionIrStep::AutomationDefinitionUpdate { .. }
                | TransactionIrStep::AutomationDefinitionDraftGuard { .. }
                | TransactionIrStep::AutomationRunMutation { .. }
                | TransactionIrStep::AutomationRunCancel { .. }
                | TransactionIrStep::AutomationRunApprove { .. }
                | TransactionIrStep::WorkerJobEnqueue { .. }
                | TransactionIrStep::WorkerJobGet { .. }
                | TransactionIrStep::WorkerJobClaimNext { .. }
                | TransactionIrStep::WorkerJobDefer { .. }
                | TransactionIrStep::WorkerJobHeartbeat { .. }
                | TransactionIrStep::WorkerJobClaimState { .. }
                | TransactionIrStep::WorkerJobRequestCancel { .. }
                | TransactionIrStep::WorkerJobComplete { .. }
                | TransactionIrStep::TaskResultCheckpoint { .. }
                | TransactionIrStep::TaskResultRecordGet { .. }
                | TransactionIrStep::TaskResultReplayGet { .. }
                | TransactionIrStep::TaskResultAbort { .. }
                | TransactionIrStep::TaskResultAbortRequested { .. }
                | TransactionIrStep::TaskResultRecoverRunning { .. }
                | TransactionIrStep::TaskResultRecoveryPending { .. }
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
    }) || ir.steps.iter().any(|step| match step {
        // Scoped live answers resolve the owned attempt and update its existing
        // recovery sizing; ordinary settled projection edits retain their scope.
        TransactionIrStep::TurnProjectionUpdate { human_guidance_answer_json, .. } =>
            human_guidance_answer_json.is_some(),
        // A fenced stream append resolves attempt identity claims in the
        // tenant-global namespace, like the turn steps above.
        TransactionIrStep::IndexedStreamAppend {
            require_attempts_live,
            ..
        } => !require_attempts_live.is_empty(),
        TransactionIrStep::IndexedStreamAppendBatch { items, .. } => items
            .iter()
            .any(|item| !item.require_attempts_live.is_empty()),
        _ => false,
    });
    // Only document IR admits its shared capacity namespace. Unrelated semantic
    // requests retain their existing scope set and allocation cost.
    let mut transaction = if ir
        .steps
        .iter()
        .any(|step| matches!(step, TransactionIrStep::PaperDocument { .. }))
    {
        database.begin_with_paper_document_scope(ir.tenant_id, ir.owner_user_id)?
    } else if needs_identity_claim_scopes {
        database.begin_with_identity_claim_scopes(ir.tenant_id, ir.owner_user_id)?
    } else {
        database.begin(ir.tenant_id, ir.owner_user_id)?
    };

    if let Some(response) = lookup_receipt(database, &mut transaction, ir)? {
        return Ok(PreparedTransactionIr { response: IrResponse::unverified(response), transaction: None });
    }
    // A finite rotation install can fence writes for many request arrivals.
    // Do not repeatedly materialize projections and stage blobs only to fail
    // at commit. Replay identity is checked first; strict durability and the
    // commit-time fence remain authoritative after this early admission.
    if ir.operation_kind.mutates_state() {
        database.require_write_admission()?;
    }
    if ir.operation == "turn.perception.record" {
        database.exempt_content_free_diagnostic_from_logical_outbox(&mut transaction)?;
    }
    let mut slots = IrSlots::new();
    let _steps_timer = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_IR_STEPS);
    let steps = match execution {
        IrStepExecution::Sequential => ir.steps.as_slice(),
        #[cfg(test)]
        IrStepExecution::ExperimentalTurnFrame => {
            for (slot, response) in crate::turn::execution_frame::record(
                database, &mut transaction, &ir.steps, &mut deadline_elapsed,
            )? {
                slots[slot] = Some(Some(response));
            }
            &[]
        }
    };
    for step in steps {
        match step {
            TransactionIrStep::Queue { request, slot } => {
                slots[*slot as usize] =
                    Some(crate::queue::execute(database, &mut transaction, request)?);
            }
            TransactionIrStep::ModelContinuation { request, slot } => {
                slots[*slot as usize] = Some(crate::model_continuation::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::Invocation { request, slot } => {
                slots[*slot as usize] = Some(crate::invocation::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
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
                selector,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::artifact::tool_read(
                    database,
                    &mut transaction,
                    artifact_ref,
                    *now_ms,
                    offset,
                    *limit,
                    selector,
                )?);
            }
            TransactionIrStep::ToolResultArtifactSearch {
                artifact_ref,
                query,
                now_ms,
                cursor,
                selector,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::artifact::tool_search(
                    database,
                    &mut transaction,
                    artifact_ref,
                    query,
                    *now_ms,
                    cursor,
                    *limit,
                    selector,
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
                archive_scope,
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
                        archive_scope: archive_scope.clone(),
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
                archive_scope,
                limit,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::list(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::ListRequest {
                        conversation_id: conversation_id.clone(),
                        archive_scope: archive_scope.clone(),
                        limit: *limit,
                    },
                )?));
            }
            TransactionIrStep::CompactionArchiveGet {
                conversation_id,
                archive_id,
                archive_scope,
                include_messages,
                include_post_messages,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::compaction_archive::get(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::GetRequest {
                        conversation_id: conversation_id.clone(),
                        archive_id: archive_id.clone(),
                        archive_scope: archive_scope.clone(),
                        include_messages: *include_messages,
                        include_post_messages: *include_post_messages,
                    },
                )?);
            }
            TransactionIrStep::CompactionArchiveUpdateSummary {
                archive_id,
                archive_scope,
                summary,
                tokens_after,
                msgs_after,
                receipt_json,
                post_messages_json,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::update_summary(
                    database,
                    &mut transaction,
                    &crate::compaction_archive::UpdateSummaryRequest {
                        archive_id: archive_id.clone(),
                        archive_scope: archive_scope.clone(),
                        summary: summary.clone(),
                        tokens_after: *tokens_after,
                        msgs_after: *msgs_after,
                        receipt_json: receipt_json.clone(),
                        post_messages_json: post_messages_json.clone(),
                        committed_at_ms: *committed_at_ms,
                    },
                )?));
            }
            TransactionIrStep::CompactionArchiveDeleteConversation {
                conversation_id,
                archive_scope,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::delete_conversation(
                    database,
                    &mut transaction,
                    conversation_id,
                    archive_scope,
                )?));
            }
            TransactionIrStep::CompactionArchivePrune {
                conversation_id,
                archive_scope,
                keep,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::compaction_archive::prune(
                    database,
                    &mut transaction,
                    conversation_id,
                    archive_scope,
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
            TransactionIrStep::ConversationDiagnostic { request, slot } => {
                slots[*slot as usize] = Some(crate::turn::diagnostic::read(
                    database,
                    &mut transaction,
                    request,
                    &mut deadline_elapsed,
                )?);
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
                    &mut deadline_elapsed,
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
                    &mut deadline_elapsed,
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
                TransactionIrStep::AutomationSkillSourceUpdate {request,slot} => {
                slots[*slot as usize]=Some(Some(crate::automation_skill_source::execute(database,&mut transaction,request)?));
            }
                TransactionIrStep::AutomationProjectUpdate {request,slot} => {
                slots[*slot as usize]=Some(Some(crate::automation_project::execute(database,&mut transaction,request)?));
            }
                TransactionIrStep::AutomationDefinitionUpdate {request,slot} => {
                slots[*slot as usize]=Some(Some(crate::automation_definition::execute(database,&mut transaction,request)?));
            }
            TransactionIrStep::AutomationEventSettle { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::automation_events::execute(
                    database, &mut transaction, request,
                )?));
            }
            TransactionIrStep::AutomationDefinitionDraftGuard {key} => {
                crate::automation_definition::guard_draft(database,&mut transaction,key)?;
            }
            TransactionIrStep::AutomationRunMutation { request, advance, slot } => {
                slots[*slot as usize] = Some(Some(crate::automation_run::execute(
                    database, &mut transaction, request, *advance,
                )?));
            }
            TransactionIrStep::AutomationRunCancel { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::automation_run::cancel(database, &mut transaction, request)?));
            }
            TransactionIrStep::AutomationRunApprove { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::automation_approval::approve(database, &mut transaction, request)?));
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
            TransactionIrStep::WorkerJobDefer {
                task_id,
                worker_id,
                fencing_token,
                now_ms,
                available_at_ms,
                replay_cursor,
                observed_cancel_sequence,
                observed_wake_sequence,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::worker_job::defer(
                    database,
                    &mut transaction,
                    crate::worker_job::DeferRequest {
                        task_id,
                        worker_id,
                        fence: *fencing_token,
                        now_ms: *now_ms,
                        available_at_ms: *available_at_ms,
                        replay_cursor: *replay_cursor,
                        observed_cancel_sequence: *observed_cancel_sequence,
                        observed_wake_sequence: *observed_wake_sequence,
                    },
                )?));
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
                projection_timestamp,
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
                slots.set_settled_append_response(*slot as usize, crate::turn::append_settled_with_receipt_proof(
                    database,
                    &mut transaction,
                    &crate::turn::AppendSettledRequest {
                        conversation_id: conversation_id.clone(),
                        actor: actor.clone(),
                        status: status.clone(),
                        projection_json: projection_json.clone(),
                        projection_timestamp: projection_timestamp.clone(),
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
                )?);
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
            TransactionIrStep::TurnContextRead { conversation_id, turn_id, replay, after_turn_id, slot } => {
                slots.set_response(*slot as usize, crate::turn::model_context::read(
                    database, &mut transaction, conversation_id, turn_id, *replay,
                    after_turn_id.as_deref())?.map(IrResponse::from_encoded));
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
                    &mut deadline_elapsed,
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
            TransactionIrStep::TurnAttemptGet { attempt_id, slot, include_execution_config } => {
                slots[*slot as usize] = Some(crate::turn::attempt_get_with_config(
                    database,
                    &mut transaction,
                    attempt_id,
                    *include_execution_config,
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
                automation_context,
                committed_at_ms,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(crate::turn::attempt_start_with_automation(
                    database,
                    &mut transaction,
                    attempt_id,
                    task_id,
                    *committed_at_ms,
                    automation_context.as_ref(),
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
                    &mut deadline_elapsed,
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
                preserve_activity,
                human_guidance_answer_json,
                conversation_id,
                turn_id,
                projection_json,
                expected_projection_revision,
                updated_at_ms,
                committed_at_ms,
                slot,
            } => {
                if let Some(answer_json) = human_guidance_answer_json {
                    slots[*slot as usize] = Some(Some(crate::turn::answer_guidance(
                        database, &mut transaction, conversation_id, turn_id, answer_json, *committed_at_ms,
                    )?));
                    continue;
                }
                slots[*slot as usize] = Some(Some(crate::turn::update_projection(
                    database,
                    &mut transaction,
                    &crate::turn::ProjectionUpdateRequest {
                        preserve_activity: *preserve_activity,
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
                max_projection_bytes,
                include_artifact_hint,
                slot,
            } => {
                let mut deadline_seen = false;
                slots.set_response(*slot as usize, crate::turn::sync_snapshot_with_proof(
                    database,
                    &mut transaction,
                    conversation_id,
                    *turn_limit,
                    *max_projection_bytes,
                    *include_artifact_hint,
                    &mut || {
                        let elapsed = deadline_elapsed();
                        deadline_seen |= elapsed;
                        elapsed
                    },
                )?.map(IrResponse::from_encoded));
                snapshot_read_complete = !deadline_seen;
            }
            TransactionIrStep::TurnSyncRoundDirectory {
                conversation_id, lane_id, offset, limit, sync_sequence, anchor_turn_id, slot,
            } => {
                slots[*slot as usize] = Some(crate::turn::sync_round_directory(database,
                    &mut transaction, conversation_id, lane_id, *offset, *limit,
                    *sync_sequence, anchor_turn_id, &mut deadline_elapsed)?);
            }
            TransactionIrStep::TurnSyncPage {
                conversation_id,
                lane_id,
                before_ordinal,
                limit,
                sync_sequence,
                max_projection_bytes,
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
                    *max_projection_bytes,
                    &mut deadline_elapsed,
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
                    &mut deadline_elapsed,
                )?);
            }
            TransactionIrStep::TurnSyncPrune { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::sync_prune(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnCleanup { request, slot } => {
                slots[*slot as usize] = Some(Some(crate::turn::cleanup(
                    database,
                    &mut transaction,
                    request,
                )?));
            }
            TransactionIrStep::TurnSearchBackfillGlobal { request, slot } => {
                slots[*slot as usize] = Some(Some(
                    crate::semantic_executor::search_backfill_global::execute(
                        database,
                        &mut transaction,
                        request,
                    )?,
                ));
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
                slots.set_projection(*slot as usize, crate::versioned_document::get_with_response_proof(
                    database, &mut transaction, key, namespace, logical_key,
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
            TransactionIrStep::TaskResultRecordGet {
                task_id,
                requested_user_id,
                slot,
            } => {
                slots[*slot as usize] = Some(crate::task_result::record_get(
                    database,
                    &mut transaction,
                    task_id,
                    *requested_user_id,
                )?);
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
            TransactionIrStep::TaskResultRecoveryPending { slot } => {
                slots[*slot as usize] = Some(Some(crate::task_result::recovery_pending(
                    database,
                    &mut transaction,
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

                include_value,
                end,
                namespace,
                limit,
                reverse,
                slot,
            } => {
                let projection = if *include_value {
                    crate::versioned_document::list_direction_with_response_proof(
                        database, &mut transaction, start, end, namespace, *limit, *reverse,
                    )?
                } else {
                    crate::versioned_document::list_headers_direction_with_response_proof(
                        database, &mut transaction, start, end, namespace, *limit, *reverse,
                    )?
                };
                slots.set_projection(*slot as usize, Some(projection));
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
            TransactionIrStep::PluginRowOperation { request, slot } => {
                slots[*slot as usize] = Some(crate::plugin_rows::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
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
            TransactionIrStep::PaperBuildArtifact { request, slot } => {
                slots[*slot as usize] = Some(crate::paper_build_artifact::execute(
                    database,
                    &mut transaction,
                    request,
                )?);
            }
            TransactionIrStep::PaperDocument { request, slot } => {
                slots[*slot as usize] = Some(crate::paper_document::execute(
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
            TransactionIrStep::ProjectBrainRecoverySnapshot { slot, cursor } => {
                slots[*slot as usize] = Some(Some(crate::project_brain::recovery_snapshot(
                    database,
                    &mut transaction,
                    cursor.as_ref(),
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
                require_attempts_live,
                slot,
            } => {
                slots[*slot as usize] = Some(Some(execute_fenced_indexed_append(
                    database,
                    &mut transaction,
                    &crate::indexed_stream::AppendRequest {
                        task_id: task_id.clone(),
                        application_sequence: *application_sequence,
                        event_type: event_type.clone(),
                        payload_json: payload_json.clone(),
                        created_at_ms: *created_at_ms,
                    },
                    require_attempts_live,
                )?));
            }
            TransactionIrStep::IndexedStreamAppendBatch { items, slot } => {
                if items
                    .iter()
                    .any(|item| !item.require_attempts_live.is_empty())
                {
                    // Fenced items need a per-row verdict, so run the same
                    // single-append path per item and rebuild the aggregate
                    // response shape of `indexed_stream::append_batch`.
                    let mut results = Vec::with_capacity(items.len());
                    let mut inserted = 0_usize;
                    let mut rejected = 0_usize;
                    for item in items {
                        let encoded = execute_fenced_indexed_append(
                            database,
                            &mut transaction,
                            &crate::indexed_stream::AppendRequest {
                                task_id: item.task_id.clone(),
                                application_sequence: item.application_sequence,
                                event_type: item.event_type.clone(),
                                payload_json: item.payload_json.clone(),
                                created_at_ms: item.created_at_ms,
                            },
                            &item.require_attempts_live,
                        )?;
                        let result: Value = serde_json::from_slice(&encoded)
                            .map_err(|_| invalid_input("indexed event append result is invalid"))?;
                        inserted += usize::from(result["inserted"] == true);
                        rejected += usize::from(result.get("rejected").is_some());
                        results.push(result);
                    }
                    slots[*slot as usize] = Some(Some(
                        serde_json::to_vec(&json!({
                            "results": results,
                            "inserted": inserted,
                            "deduplicated": items.len() - inserted - rejected,
                            "rejected": rejected,
                        }))
                        .map_err(|_| invalid_input("indexed event batch projection failed"))?,
                    ));
                } else {
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
            }
            TransactionIrStep::IndexedStreamList {
                task_id,
                after_sequence,
                limit,
                types,
                type_prefixes,
                payload_types,
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
                        payload_types: payload_types.clone(),
                    },
                )?));
            }
            TransactionIrStep::IndexedStreamBounds { task_id, include_read_guard, types, slot } => {
                slots[*slot as usize] = Some(Some(crate::indexed_stream::bounds(
                    database,
                    &mut transaction,
                    task_id,
                    *include_read_guard,
                    types.as_deref(),
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
                        // A mutating IR always carries command effects
                        // (`TransactionIr::validate` rejects every other
                        // shape), so `committed_at_ms` is the compiling
                        // request's wall-clock now and the sweep ceiling can
                        // never run ahead of the retained entries. The
                        // fallback only preserves the pre-watermark behavior
                        // for an effect-free execution (defense in depth).
                        now_unix_ms: ir
                            .command_effects
                            .as_ref()
                            .map(|effects| effects.committed_at_ms)
                            .unwrap_or(*created_before_ms as u64),
                    },
                    &deadline_elapsed,
                )?));
            }
            TransactionIrStep::IndexedStreamLatest { task_id, metadata_only, slot } => {
                slots[*slot as usize] = Some(crate::indexed_stream::latest(
                    database,
                    &mut transaction,
                    task_id,
                    *metadata_only,
                )?);
            }
        }
    }
    drop(_steps_timer);
    let mut response = result_bytes(&ir.result, &mut slots)?;
    // This completion witness alone grants no cache eligibility. The semantic
    // boundary still checks the exact single-step IR and selected result slot.
    response.snapshot_read_complete = snapshot_read_complete;
    let Some(effects) = &ir.command_effects else {
        return Ok(PreparedTransactionIr {
            response,
            transaction: transaction.has_business_mutation().then_some(transaction),
        });
    };
    let receipt_cacheable = {
        let _policy = crate::fsync_stats::StageTimer::diagnostic(
            crate::fsync_stats::STAGE_IR_RECEIPT_RESPONSE_POLICY);
        effects.store_receipt && if response.receipt_cacheable_proven {
            crate::request_profile::work(crate::request_profile::Work::ReceiptPolicyProofReuse, 1);
            true
        } else {
            serde_json::from_slice::<serde_json::Value>(&response).map_or(true, |value| {
                !value.is_null()
                    && !value
                        .as_object()
                        .is_some_and(|object| object.get("ok") == Some(&serde_json::Value::Bool(false)))
            })
        }
    };
    if !receipt_cacheable && !transaction.has_business_mutation() {
        return Ok(PreparedTransactionIr { response, transaction: None });
    }
    if receipt_cacheable {
        let _ta_receipt = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_RECEIPT);
        database.receipt_insert(
            &mut transaction,
            &effects.command_id,
            &ir.operation,
            effects.request_digest,
            &response,
            effects.committed_at_ms,
        )?;
    }
    if database.logical_outbox_is_configured() {
        let _ta_outbox = crate::fsync_stats::StageTimer::new(crate::fsync_stats::STAGE_TA_OUTBOX);
        database.capture_outbox_for_ir(
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
    Ok(PreparedTransactionIr { response, transaction: Some(transaction) })
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
            operation: "record.get".to_owned(),
            operation_kind: StorageOperationKind::Query,
            steps,
            result,
            command_effects: None,
        }
    }

    #[test]
    fn response_proof_follows_actual_slots_and_is_invalidated_before_mutation() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let record = crate::versioned_document::record_key(7, 11, "proof", "row").unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        crate::versioned_document::put(
            &database,
            &mut transaction,
            crate::versioned_document::PutRequest {
                key: record.clone(),
                namespace: "proof".into(),
                logical_key: "row".into(),
                value_json: br#"{"items":[1,true,"text"]}"#.to_vec(),
                expected_version: Some(0),
                updated_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        let projection = crate::versioned_document::get_with_response_proof(
            &database,
            &mut transaction,
            &record,
            "proof",
            "row",
        )
        .unwrap();
        let mut slots = IrSlots::new();
        slots.set_projection(0, projection);
        let selected = TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        };
        let proven = result_bytes(&selected, &mut slots).unwrap();
        assert!(proven.json_proven());
        assert!(crate::response_json::validate(&proven).is_ok());
        assert!(proven.clone().json_proven());
        // The operation remains record.get, but a literal can never inherit
        // the previously selected slot's certificate.
        let literal =
            result_bytes(&TransactionIrResult::Literal(b"not JSON".to_vec()), &mut slots).unwrap();
        assert!(!literal.json_proven());
        assert!(crate::response_json::validate(&literal).is_err());
        assert!(slots.bytes[0].is_none());
        assert!(!slots.json_proven[0]);
        slots.set_projection(0, crate::versioned_document::get_with_response_proof(
            &database, &mut transaction, &record, "proof", "row").unwrap());
        slots[0].as_mut().unwrap().as_mut().unwrap()[0] = b'!';
        let mutated = result_bytes(&selected, &mut slots).unwrap();
        assert!(!mutated.json_proven());
        assert!(crate::response_json::validate(&mutated).is_err());
        slots[0] = Some(None);
        let missing = result_bytes(&selected, &mut slots).unwrap();
        assert!(!missing.json_proven());
        assert_eq!(missing.into_bytes(), b"null");
        slots[0] = Some(Some(b"{}".to_vec()));
        assert!(!result_bytes(&selected, &mut slots).unwrap().json_proven());
    }

    #[test]
    fn final_result_transfers_large_allocation_and_preserves_missing_and_invalid_slots() {
        let mut slots = IrSlots::new();
        let bytes = vec![b' '; 1024 * 1024];
        let pointer = bytes.as_ptr();
        let capacity = bytes.capacity();
        slots[0] = Some(Some(bytes));
        let selected = TransactionIrResult::SlotOrLiteral { slot: 0, missing: b"null".to_vec() };
        let response = result_bytes(&selected, &mut slots).unwrap();
        assert_eq!(response.bytes.as_ptr(), pointer);
        assert_eq!(response.bytes.capacity(), capacity);
        assert_eq!(response.len(), 1024 * 1024);
        assert!(!response.json_proven());
        assert!(result_bytes(&selected, &mut slots).is_err());
        slots[0] = Some(None);
        assert_eq!(result_bytes(&selected, &mut slots).unwrap().into_bytes(), b"null");
        let invalid = TransactionIrResult::SlotOrLiteral { slot: usize::MAX, missing: Vec::new() };
        assert!(result_bytes(&invalid, &mut slots).is_err());
    }

    fn settled_receipt_response_fixture() -> crate::turn::SettledAppendResponse {
        let directory = tempfile::tempdir().unwrap();
        let database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled_with_receipt_proof(&database, &mut transaction,
            &crate::turn::AppendSettledRequest {
                conversation_id: "receipt-producer".into(), actor: "human".into(),
                status: "completed".into(), projection_json: br#"{"content":"escaped \n text","ok":false}"#.to_vec(),
                projection_timestamp: None, settlement_json: br#"{"outcome":"completed"}"#.to_vec(),
                lane_id: "main".into(), command_id: "producer-command".into(),
                kind: "message".into(), run_id: String::new(), turn_id: "producer-turn".into(),
                attempt_id: None, created_at_ms: 1000, committed_at_ms: 1000,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true, title: "Producer".into(), settings_json: b"{}".to_vec(), created_at_ms: 1000,
                },
            }).unwrap()
    }

    #[test]
    fn receipt_proof_follows_only_selected_immutable_producer_bytes() {
        let selected = TransactionIrResult::SlotOrLiteral { slot: 0, missing: b"null".to_vec() };
        let mut slots = IrSlots::new();
        slots.set_settled_append_response(0, settled_receipt_response_fixture());
        let response = result_bytes(&selected, &mut slots).unwrap();
        assert!(response.receipt_cacheable_proven);
        assert!(!response.json_proven(), "receipt policy is not an integrity proof");
        crate::response_json::validate(&response).unwrap();
        let parsed: Value = serde_json::from_slice(&response).unwrap();
        assert!(parsed.get("ok").is_none());
        assert_eq!(parsed["turn"]["projection"]["ok"], Value::Bool(false));
        assert!(!slots.receipt_cacheable_proven[0]);
        assert!(result_bytes(&selected, &mut slots).is_err());
        let exported = response.into_bytes();
        assert!(!IrResponse::unverified(exported).receipt_cacheable_proven);

        slots.set_settled_append_response(0, settled_receipt_response_fixture());
        let replacement = slots[0].as_mut().unwrap().as_mut().unwrap();
        replacement.clear(); replacement.extend_from_slice(br#"{"ok":false}"#);
        assert!(!result_bytes(&selected, &mut slots).unwrap().receipt_cacheable_proven);

        slots.set_settled_append_response(0, settled_receipt_response_fixture());
        let literal = result_bytes(&TransactionIrResult::Literal(b"null".to_vec()), &mut slots).unwrap();
        assert!(!literal.receipt_cacheable_proven);
        slots[1] = Some(Some(br#"{"ok":false}"#.to_vec()));
        let alternate = TransactionIrResult::SlotOrLiteral { slot: 1, missing: b"null".to_vec() };
        assert!(!result_bytes(&alternate, &mut slots).unwrap().receipt_cacheable_proven);
        slots.set_projection(0, None);
        assert!(!result_bytes(&selected, &mut slots).unwrap().receipt_cacheable_proven);
        eprintln!("receipt_resource_layout: slots={} response={} slot_payloads={} slot_count={}",
            std::mem::size_of::<IrSlots>(), std::mem::size_of::<IrResponse>(),
            std::mem::size_of::<SparseSlotValues<Option<Option<Vec<u8>>>>>(), slots.bytes.len());
    }

    #[test]
    fn response_proof_does_not_survive_serialized_receipt_replay() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let record = crate::versioned_document::record_key(7, 11, "proof", "row").unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        crate::versioned_document::put(
            &database,
            &mut transaction,
            crate::versioned_document::PutRequest {
                key: record.clone(),
                namespace: "proof".into(),
                logical_key: "row".into(),
                value_json: b"[1,2]".to_vec(),
                expected_version: Some(0),
                updated_at_ms: 1,
            },
        )
        .unwrap();
        database.commit(transaction).unwrap();
        let mut command =
            stream_command(StreamKey::new(7, 11, "task_event", b"proof-receipt").unwrap());
        command.steps.push(TransactionIrStep::VersionedDocumentGet {
            key: record,
            namespace: "proof".into(),
            logical_key: "row".into(),
            slot: 0,
        });
        command.result = TransactionIrResult::SlotOrLiteral {
            slot: 0,
            missing: b"null".to_vec(),
        };
        command.command_effects.as_mut().unwrap().store_receipt = true;
        let initial = execute_transaction_ir_with_json_proof(
            &mut database,
            &command,
            &mut Vec::new(),
            Duration::MAX,
        )
        .unwrap();
        assert!(initial.json_proven());
        let before = database.wal_len().unwrap();
        let replay = execute_transaction_ir_with_json_proof(
            &mut database,
            &command,
            &mut Vec::new(),
            Duration::MAX,
        )
        .unwrap();
        assert!(!replay.json_proven());
        assert_eq!(initial.into_bytes(), replay.into_bytes());
        assert_eq!(database.wal_len().unwrap(), before);
    }

    #[test]
    fn response_proof_is_not_inferred_from_raw_entity_reads_or_operation_names() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let mut transaction = database.begin(7, 11).unwrap();
        database
            .entity_put(&mut transaction, key(), b"not JSON".to_vec())
            .unwrap();
        database.commit(transaction).unwrap();
        let ir = query(
            vec![TransactionIrStep::EntityRead {
                key: key(),
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        );
        let result =
            execute_transaction_ir_with_json_proof(&mut database, &ir, &mut Vec::new(), Duration::MAX)
                .unwrap();
        assert!(!result.json_proven());
        assert_eq!(result.into_bytes(), b"not JSON");
    }

    #[test]
    fn turn_sync_changes_honors_the_execution_deadline_budget() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        for index in 0..2_u64 {
            let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
            crate::turn::append_settled(
                &database,
                &mut seed,
                &crate::turn::AppendSettledRequest {
                    conversation_id: "deadline-conversation".to_owned(),
                    actor: "assistant".to_owned(),
                    status: "pending".to_owned(),
                    projection_json:
                        br#"{"content":"","thinking":"","segments":[],"toolRounds":[]}"#.to_vec(),
                    projection_timestamp: None,
                    settlement_json: b"{}".to_vec(),
                    lane_id: "main".to_owned(),
                    command_id: format!("command-deadline-{index}"),
                    kind: "reply".to_owned(),
                    run_id: String::new(),
                    turn_id: format!("deadline-turn-{index}"),
                    attempt_id: Some(format!("deadline-attempt-{index}")),
                    created_at_ms: 1,
                    committed_at_ms: 1,
                    defaults: crate::conversation_header::TurnDefaults {
                        allow_create: true,
                        title: "Deadline fixture".to_owned(),
                        settings_json: b"{}".to_vec(),
                        created_at_ms: 1,
                    },
                },
            )
            .unwrap();
            database.commit(seed).unwrap();
        }
        let ir = query(
            vec![TransactionIrStep::TurnSyncChanges {
                conversation_id: "deadline-conversation".to_owned(),
                after: 0,
                limit: 500,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"null".to_vec(),
            },
        );
        // A spent budget reaches the read loop as `deadline_elapsed` instead of
        // a silent caught-up page; an unbounded budget returns the full page.
        let error = execute_transaction_ir_with_deadline(
            &mut database,
            &ir,
            &mut Vec::new(),
            Duration::ZERO,
        )
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
        let full = execute_transaction_ir_with_deadline(
            &mut database,
            &ir,
            &mut Vec::new(),
            Duration::MAX,
        )
        .unwrap();
        let page: Value = serde_json::from_slice(&full).unwrap();
        assert_eq!(page["events"].as_array().unwrap().len(), 2);
        assert_eq!(page["hasMore"], json!(false));
    }

    fn stream_command(key: StreamKey) -> TransactionIr {
        TransactionIr {
            tenant_id: 7,
            owner_user_id: 11,
            operation: "event.append".to_owned(),
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
                legacy_receipt_owner_bound: false,
                command_id: "natural-event-key".to_owned(),
                request_digest: [4; 32],
                alternate_request_digest: None,
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
    fn rotation_admission_precedes_semantic_work_but_preserves_receipt_replay_and_reads() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        let stream = StreamKey::new(7, 11, "task_event", b"rotation-event").unwrap();
        let mut command = stream_command(stream);
        command.command_effects.as_mut().unwrap().store_receipt = true;
        let committed = execute_transaction_ir(&mut database, &command).unwrap();
        let engine = database.engine_mut_for_test();
        let store = engine.block_store_shared();
        let registration = store.register_background_packer().unwrap();
        let prepared = match engine.try_prepare_rotation_checkpoint().unwrap() {
            crate::engine::RotationCheckpointPreparation::Ready(prepared) => prepared,
            other => panic!("expected rotation, got {other:?}"),
        };
        let outcome = crate::engine::encode_rotation_segments(&prepared).unwrap();
        // Capture a legacy single-WAL snapshot before attaching the driver:
        // its install still retires window writes and requires this fence.
        engine.attach_rotation_driver();
        engine.commit_transaction(b"window", &[b"pending payload"]).unwrap();
        let mut adoption = engine.prepare_pack_publication_for_test();
        assert_eq!(engine.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert!(engine.online_rotation_blocks_writes());
        let wal_before = database.wal_len().unwrap();
        let staged_before = store.pending_staged_bytes().unwrap();

        assert_eq!(execute_transaction_ir(&mut database, &command).unwrap(), committed);
        let mut conflicting_replay = command.clone();
        conflicting_replay.command_effects.as_mut().unwrap().request_digest = [99; 32];
        assert_eq!(execute_transaction_ir(&mut database, &conflicting_replay).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert_eq!(execute_transaction_ir(&mut database, &query(Vec::new(),
            TransactionIrResult::Literal(b"readable".to_vec()))).unwrap(), b"readable");

        let mut fresh = command.clone();
        fresh.command_effects.as_mut().unwrap().command_id = "new-command".to_owned();
        // The old stream position is deliberately stale. While fenced, the
        // write must be parked before even evaluating this semantic conflict.
        assert_eq!(execute_transaction_ir(&mut database, &fresh).unwrap_err().kind(), io::ErrorKind::ResourceBusy);
        assert_eq!(database.wal_len().unwrap(), wal_before);
        assert_eq!(store.pending_staged_bytes().unwrap(), staged_before);

        let published = crate::engine::publish_prepared_pack_adoption(&mut adoption);
        database.engine_mut_for_test().finish_pack_adoption(adoption, published).unwrap();
        drop(registration);
        let crate::engine::RotationWork::Deferred(prepared, outcome) =
            database.take_prepared_rotation().unwrap().unwrap() else { panic!() };
        database.finish_rotation_checkpoint(prepared, Ok(outcome)).unwrap();
        assert_eq!(execute_transaction_ir(&mut database, &fresh).unwrap_err().kind(), io::ErrorKind::WouldBlock);
        assert_eq!(execute_transaction_ir(&mut database, &command).unwrap(), committed);
    }

    #[test]
    fn ordered_step_demand_exceeds_former_quota_with_late_conflict_rollback_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let stream = StreamKey::new(7, 11, "task_event", b"step-demand").unwrap();
        let make_key = |ordinal: u64| EntityKey::new(7, 11, "ir-test", &ordinal.to_be_bytes()).unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut command = stream_command(stream.clone());
            let mut tail = command.steps.pop().unwrap();
            if let TransactionIrStep::StreamAppend { expected_next_sequence, .. } = &mut tail {
                *expected_next_sequence = 2;
            }
            command.steps = (0..1001_u64).map(|ordinal| TransactionIrStep::EntityPut {
                key: make_key(ordinal), value: ordinal.to_be_bytes().to_vec(),
                condition: EntityWriteCondition::Always,
            }).collect();
            command.steps.push(tail);
            command.validate().unwrap();
            let before = database.wal_len().unwrap();
            assert_eq!(execute_transaction_ir(&mut database, &command).unwrap_err().kind(), io::ErrorKind::WouldBlock);
            assert_eq!(database.wal_len().unwrap(), before);
            let mut read = database.begin(7, 11).unwrap();
            for ordinal in 0..1001 { assert_eq!(database.entity_get(&mut read, &make_key(ordinal)).unwrap(), None); }
            drop(read);
            if let TransactionIrStep::StreamAppend { expected_next_sequence, .. } = command.steps.last_mut().unwrap() {
                *expected_next_sequence = 1;
            }
            assert_eq!(execute_transaction_ir(&mut database, &command).unwrap(), br#"{"inserted":true}"#);
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        for ordinal in 0..1001_u64 {
            assert_eq!(database.entity_get(&mut read, &make_key(ordinal)).unwrap(), Some(ordinal.to_be_bytes().to_vec()));
        }
        assert_eq!(database.stream_read(7, 11, &stream, 1, 2).unwrap().events.len(), 1);
    }

    #[test]
    fn sparse_slot_demand_exceeds_former_capacity_and_large_identifier_reopens_exactly() {
        let directory = tempfile::tempdir().unwrap();
        let source_key = |ordinal: u64| EntityKey::new(7, 11, "ir-test", &ordinal.to_be_bytes()).unwrap();
        let target_key = |ordinal: u64| EntityKey::new(7, 11, "ir-test", format!("target-{ordinal}").as_bytes()).unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut seed = database.begin(7, 11).unwrap();
            for ordinal in 0..513_u64 {
                database.entity_put(&mut seed, source_key(ordinal), ordinal.to_be_bytes().to_vec()).unwrap();
            }
            database.commit(seed).unwrap();
            let stream = StreamKey::new(7, 11, "task_event", b"slot-demand").unwrap();
            let mut command = stream_command(stream);
            command.steps.clear();
            for ordinal in 0..513_u64 {
                command.steps.push(TransactionIrStep::EntityRead { key: source_key(ordinal), slot: ordinal as usize });
                command.steps.push(TransactionIrStep::EntityPut { key: target_key(ordinal),
                    value: ordinal.to_be_bytes().to_vec(), condition: EntityWriteCondition::SlotPresent(ordinal as usize) });
            }
            command.steps.push(TransactionIrStep::EntityRead { key: source_key(512), slot: usize::MAX });
            command.result = TransactionIrResult::SlotOrLiteral { slot: usize::MAX, missing: Vec::new() };
            command.validate().unwrap();
            let mut duplicate = command.clone();
            duplicate.steps.push(TransactionIrStep::EntityRead { key: source_key(0), slot: 0 });
            assert!(duplicate.validate().is_err());
            let mut unread = command.clone();
            unread.steps.push(TransactionIrStep::EntityPut { key: target_key(999), value: Vec::new(),
                condition: EntityWriteCondition::SlotPresent(999) });
            assert!(unread.validate().is_err());
            assert_eq!(execute_transaction_ir(&mut database, &command).unwrap(), 512_u64.to_be_bytes());
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        for ordinal in 0..513_u64 {
            assert_eq!(database.entity_get(&mut read, &target_key(ordinal)).unwrap(), Some(ordinal.to_be_bytes().to_vec()));
        }
    }

    #[test]
    fn sparse_slot_proof_and_residency_follow_touched_identifiers() {
        let mut slots = IrSlots::new();
        assert!(slots.bytes.get(usize::MAX).is_none());
        assert_eq!(slots.bytes.len(), 0);
        slots.set_settled_append_response(usize::MAX, settled_receipt_response_fixture());
        assert_eq!(slots.bytes.len(), 1);
        let selected = TransactionIrResult::SlotOrLiteral { slot: usize::MAX, missing: Vec::new() };
        assert!(slots.receipt_cacheable_proven[usize::MAX]);
        slots[usize::MAX].as_mut().unwrap().as_mut().unwrap()[0] = b'!';
        assert!(!slots.receipt_cacheable_proven[usize::MAX]);
        assert!(!result_bytes(&selected, &mut slots).unwrap().receipt_cacheable_proven);
        assert_eq!(slots.bytes.len(), 1);
        eprintln!("sparse_ir_slots: resident_header_bytes={} touched_slots={}",
            std::mem::size_of::<IrSlots>(), slots.bytes.len());
    }

    #[test]
    fn literal_demand_exceeds_former_aggregate_quota_with_atomic_rollback_and_reopen() {
        let directory = tempfile::tempdir().unwrap();
        let make_key = |ordinal: u64| EntityKey::new(7, 11, "ir-test", &ordinal.to_be_bytes()).unwrap();
        let stream = StreamKey::new(7, 11, "task_event", b"literal-demand").unwrap();
        {
            let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
            let mut command = stream_command(stream.clone());
            let mut tail = command.steps.pop().unwrap();
            if let TransactionIrStep::StreamAppend { expected_next_sequence, .. } = &mut tail {
                *expected_next_sequence = 2;
            }
            command.steps = (0..1501_u64).map(|ordinal| TransactionIrStep::EntityPut {
                key: make_key(ordinal), value: vec![(ordinal % 251) as u8; 7000],
                condition: EntityWriteCondition::Always,
            }).collect();
            command.steps.push(tail);
            command.result = TransactionIrResult::Literal(vec![b'x'; 9 * 1024 * 1024]);
            command.validate().unwrap();
            let before = database.wal_len().unwrap();
            assert_eq!(execute_transaction_ir(&mut database, &command).unwrap_err().kind(), io::ErrorKind::WouldBlock);
            assert_eq!(database.wal_len().unwrap(), before);
            let mut read = database.begin(7, 11).unwrap();
            for ordinal in 0..1501 { assert!(database.entity_get(&mut read, &make_key(ordinal)).unwrap().is_none()); }
            drop(read);
            if let TransactionIrStep::StreamAppend { expected_next_sequence, .. } = command.steps.last_mut().unwrap() {
                *expected_next_sequence = 1;
            }
            let response = execute_transaction_ir(&mut database, &command).unwrap();
            if let TransactionIrResult::Literal(expected) = &command.result {
                assert_eq!(response.len(), expected.len());
                assert_eq!(blake3::hash(&response), blake3::hash(expected));
            } else { panic!("literal fixture changed"); }
        }
        let database = AuthorityDatabase::open(directory.path()).unwrap();
        let mut read = database.begin(7, 11).unwrap();
        for ordinal in 0..1501_u64 {
            let value = database.entity_get(&mut read, &make_key(ordinal)).unwrap().unwrap();
            assert_eq!(value.len(), 7000);
            assert!(value.iter().all(|byte| *byte == (ordinal % 251) as u8));
        }
        assert_eq!(database.stream_read(7, 11, &stream, 1, 2).unwrap().events.len(), 1);
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

    fn fenced_item(
        task_id: &str,
        sequence: u64,
        require_attempts_live: &[&str],
    ) -> IndexedStreamAppendItem {
        IndexedStreamAppendItem {
            task_id: task_id.to_owned(),
            application_sequence: sequence,
            event_type: "delta".to_owned(),
            payload_json: br#"{"type":"delta","text":"x"}"#.to_vec(),
            created_at_ms: 2_000,
            require_attempts_live: require_attempts_live
                .iter()
                .map(|id| (*id).to_owned())
                .collect(),
        }
    }

    fn fenced_step(
        task_id: &str,
        sequence: u64,
        require_attempts_live: &[&str],
    ) -> TransactionIrStep {
        let item = fenced_item(task_id, sequence, require_attempts_live);
        TransactionIrStep::IndexedStreamAppend {
            task_id: item.task_id,
            application_sequence: item.application_sequence,
            event_type: item.event_type,
            payload_json: item.payload_json,
            created_at_ms: item.created_at_ms,
            require_attempts_live: item.require_attempts_live,
            slot: 0,
        }
    }

    fn fenced_append_command(command_id: &str, step: TransactionIrStep) -> TransactionIr {
        TransactionIr {
            tenant_id: 7,
            owner_user_id: 11,
            operation: "event.append".to_owned(),
            operation_kind: StorageOperationKind::Command,
            steps: vec![step],
            result: TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: Vec::new(),
            },
            command_effects: Some(TransactionIrCommandEffects {
                legacy_receipt_owner_bound: false,
                command_id: command_id.to_owned(),
                request_digest: [6; 32],
                alternate_request_digest: None,
                request_id: format!("request-{command_id}"),
                schema_version: 57,
                registry_version: 37,
                committed_at_ms: 2_000,
                outbox_payload: br#"{"operation":"event.append"}"#.to_vec(),
                store_receipt: false,
            }),
        }
    }

    fn seed_fence_attempt(database: &mut AuthorityDatabase, attempt_id: &str, status: &str) {
        let mut seed = database.begin_with_identity_claim_scopes(7, 11).unwrap();
        crate::turn::append_settled(
            database,
            &mut seed,
            &crate::turn::AppendSettledRequest {
                conversation_id: format!("fence-conversation-{attempt_id}"),
                actor: "assistant".to_owned(),
                status: status.to_owned(),
                projection_json: br#"{"content":"","thinking":"","segments":[],"toolRounds":[]}"#
                    .to_vec(),
                projection_timestamp: None,
                settlement_json: b"{}".to_vec(),
                lane_id: "main".to_owned(),
                command_id: format!("seed-{attempt_id}"),
                kind: "reply".to_owned(),
                run_id: String::new(),
                turn_id: format!("turn-{attempt_id}"),
                attempt_id: Some(attempt_id.to_owned()),
                created_at_ms: 1,
                committed_at_ms: 1,
                defaults: crate::conversation_header::TurnDefaults {
                    allow_create: true,
                    title: "Fence fixture".to_owned(),
                    settings_json: b"{}".to_vec(),
                    created_at_ms: 1,
                },
            },
        )
        .unwrap();
        database.commit(seed).unwrap();
    }

    #[test]
    fn fenced_indexed_append_enforces_attempt_liveness_in_transaction() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_fence_attempt(&mut database, "fence-live-attempt", "pending");
        seed_fence_attempt(&mut database, "fence-settled-attempt", "completed");

        // A live attempt fence inserts exactly like an unfenced append.
        let live = fenced_append_command(
            "fence-live",
            fenced_step("fence-task-live", 1, &["fence-live-attempt"]),
        );
        let live_result: Value =
            serde_json::from_slice(&execute_transaction_ir(&mut database, &live).unwrap()).unwrap();
        assert_eq!(live_result["inserted"], true);
        assert!(live_result.get("rejected").is_none());

        // A settled attempt rejects the row without failing the command.
        let settled = fenced_append_command(
            "fence-settled",
            fenced_step("fence-task-stale", 1, &["fence-settled-attempt"]),
        );
        let settled_result: Value =
            serde_json::from_slice(&execute_transaction_ir(&mut database, &settled).unwrap())
                .unwrap();
        assert_eq!(settled_result["inserted"], false);
        assert_eq!(settled_result["rejected"], "attempt_not_live");

        // A missing attempt fails closed the same way.
        let missing = fenced_append_command(
            "fence-missing",
            fenced_step("fence-task-missing", 1, &["fence-no-such-attempt"]),
        );
        let missing_result: Value =
            serde_json::from_slice(&execute_transaction_ir(&mut database, &missing).unwrap())
                .unwrap();
        assert_eq!(missing_result["inserted"], false);
        assert_eq!(missing_result["rejected"], "attempt_not_live");

        // The rejected rows never entered the stream.
        let list = query(
            vec![TransactionIrStep::IndexedStreamList {
                task_id: "fence-task-stale".to_owned(),
                after_sequence: None,
                limit: 10,
                types: Vec::new(),
                type_prefixes: Vec::new(),
                payload_types: None,
                slot: 0,
            }],
            TransactionIrResult::SlotOrLiteral {
                slot: 0,
                missing: b"[]".to_vec(),
            },
        );
        let listed: Value =
            serde_json::from_slice(&execute_transaction_ir(&mut database, &list).unwrap()).unwrap();
        assert_eq!(listed, json!([]));
    }

    #[test]
    fn fenced_indexed_append_batch_rejects_only_the_stale_rows() {
        let directory = tempfile::tempdir().unwrap();
        let mut database = AuthorityDatabase::initialize(directory.path()).unwrap();
        seed_fence_attempt(&mut database, "fence-live-attempt", "pending");
        seed_fence_attempt(&mut database, "fence-settled-attempt", "completed");

        let batch = fenced_append_command(
            "fence-batch",
            TransactionIrStep::IndexedStreamAppendBatch {
                items: vec![
                    fenced_item("fence-task-a", 1, &["fence-live-attempt"]),
                    fenced_item("fence-task-b", 1, &["fence-settled-attempt"]),
                    fenced_item("fence-task-c", 1, &[]),
                ],
                slot: 0,
            },
        );
        let result: Value =
            serde_json::from_slice(&execute_transaction_ir(&mut database, &batch).unwrap())
                .unwrap();
        assert_eq!(result["inserted"], 2);
        assert_eq!(result["deduplicated"], 0);
        assert_eq!(result["rejected"], 1);
        let results = result["results"].as_array().unwrap();
        assert_eq!(results[0]["inserted"], true);
        assert_eq!(results[1]["rejected"], "attempt_not_live");
        assert_eq!(results[2]["inserted"], true);

        // A batch with no fences at all keeps the original append_batch path.
        let plain = fenced_append_command(
            "fence-batch-plain",
            TransactionIrStep::IndexedStreamAppendBatch {
                items: vec![fenced_item("fence-task-d", 1, &[])],
                slot: 0,
            },
        );
        let plain_result: Value =
            serde_json::from_slice(&execute_transaction_ir(&mut database, &plain).unwrap())
                .unwrap();
        assert_eq!(plain_result["inserted"], 1);
        assert_eq!(plain_result["rejected"], 0);
    }

    #[test]
    fn indexed_append_fence_validation_bounds() {
        let oversized = fenced_append_command(
            "fence-overwide",
            fenced_step("fence-task", 1, &["a", "b", "c"]),
        );
        assert_eq!(
            oversized.validate().unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
        let too_long_id = fenced_append_command(
            "fence-long-id",
            fenced_step("fence-task", 1, &[&"x".repeat(129)]),
        );
        assert_eq!(
            too_long_id.validate().unwrap_err().kind(),
            io::ErrorKind::InvalidInput
        );
    }
}

#[cfg(test)]
#[path = "transaction_ir_query_cache_tests.rs"]
mod transaction_ir_query_cache_tests;
