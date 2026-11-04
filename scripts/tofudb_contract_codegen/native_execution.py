"""Project execution constants into the native IR output.
Entry: render_execution. ContractInputs owns file reads; worker data is already
validated. Automation and conversation constants preserve their output order."""

import json
from .auxiliary import automation_runtime, automation_catalog_json


def render_execution(tables, worker, inputs) -> list[str]:
    table = tables["desktop_egress_preferences"]
    worker_jobs = tables["storage_worker_jobs"]
    conversations = tables["conversations"]
    turns = tables["conversation_turns"]
    archives = tables["compaction_archives"]
    agent_field = next(
        field for field in table["fields"] if field["name"] == "agent_id"
    )
    return [
        f'pub const WORKER_JOB_STATES: &[&str] = &{json.dumps(worker["states"])};',
        *(f'pub const AUTOMATION_{name.upper()}_NAMESPACE: &str = {json.dumps(value)};'
          for name,value in sorted(inputs.contract('automation_storage_v1.schema.json')['x-namespaces'].items())),
        *(f'pub const MAX_AUTOMATION_{name}: {kind} = {inputs.contract('automation_v1.schema.json')["x-limits"][key]};'
          for name,kind,key in (("RECORD_DEPTH","usize","recordDepth"),("RECORD_NODES","usize","recordNodes"),
                                ("RECORD_BYTES","usize","recordBytes"),("SAFE_INTEGER","u64","safeIntegerMax"))),
        f'pub const MAX_AUTOMATION_STEPS: usize = {inputs.contract('automation_v1.schema.json')["properties"]["steps"]["maxItems"]};',
        f'pub const MAX_AUTOMATION_DEFINITION_BYTES: usize = {inputs.contract('automation_v1.schema.json')["x-limits"]["definitionBytes"]};',
        f'pub const MAX_AUTOMATION_PUBLICATION_REQUEST_BYTES: usize = {inputs.contract('automation_storage_v1.schema.json')["x-limits"]["publicationRequestBytes"]};',
        f'pub const AUTOMATION_PUBLICATION_METADATA_RESERVE_BYTES: usize = {inputs.contract('automation_storage_v1.schema.json')["x-limits"]["publicationMetadataReserveBytes"]};',
        f'pub const AUTOMATION_RESERVED_WORKER_TASK_KINDS: &[&str] = &{json.dumps(sorted(inputs.contract('automation_storage_v1.schema.json')["x-reservedTaskKinds"]))};',
        f'pub const MAX_AUTOMATION_CLOCK_FAILURE_KIND_CHARACTERS: usize = {inputs.contract('automation_storage_v1.schema.json')["x-clock"]["supersession"]["failureKindCharacters"]};',
        f'pub const AUTOMATION_CLOCK_JOB_CONTRACT: &str = {json.dumps(inputs.contract('automation_storage_v1.schema.json')["$defs"]["clockJob"]["properties"]["contract"]["const"])};',
        f'pub const AUTOMATION_EVENT_JOB_CONTRACT: &str = {json.dumps(inputs.contract('automation_storage_v1.schema.json')["$defs"]["taskStartedJob"]["properties"]["contract"]["const"])};',
        f'pub const MAX_WORKER_JOB_LEASE_AUTHORITY_EPOCH_CHARACTERS: usize = {worker["leaseAuthority"]["epochCharacters"]};',
        f'pub const WORKER_LEASE_INDEX_MARKER: u8 = {worker["leaseAuthority"]["leaseIndexMarker"]};',
        f'pub const AUTOMATION_EVENT_JOB_PRIORITY: u16 = {inputs.contract('automation_storage_v1.schema.json')["x-event"]["priority"]};',
        *(f'pub const MAX_AUTOMATION_EVENT_{name}: usize = {inputs.contract('automation_storage_v1.schema.json')["$defs"]["taskStartedContext"]["properties"][prop][field]};'
          for name,prop,field in (("TENANT_CHARACTERS","tenantId","maxLength"),("PROJECT_CHARACTERS","projectId","maxLength"),("ANCESTORS","automationChain","maxItems"))),
        f'pub const MAX_AUTOMATION_EVENT_ANCESTOR_CHARACTERS: usize = {inputs.contract('automation_storage_v1.schema.json')["$defs"]["taskStartedContext"]["properties"]["automationChain"]["items"]["maxLength"]};',
        *(f'pub const MAX_AUTOMATION_EVENT_{name}: usize = {inputs.contract('automation_storage_v1.schema.json')["x-event"][field]};'
          for name,field in (("BATCH_ROWS","batchRows"),("REQUEST_BYTES","settlementRequestBytes"))),
        f'pub const MAX_AUTOMATION_EVENT_ERROR_CHARACTERS: usize = {inputs.contract('automation_storage_v1.schema.json')["$defs"]["eventSettle"]["properties"]["error_kind"]["maxLength"]};',
        f'pub const AUTOMATION_PROJECT_CONTRACT: &str = {json.dumps(inputs.contract('automation_storage_v1.schema.json')["x-project"]["contract"])};',
        *(f'pub const AUTOMATION_SKILL_SOURCE_{name}: &str = {json.dumps(inputs.contract('automation_storage_v1.schema.json')["x-skillSource"][field])};'
          for name,field in (("CONTRACT","contract"),("ID","id"),("CREDENTIAL_SCOPE_DOMAIN","credentialScopeDomain"))),
        f'pub const AUTOMATION_SKILL_SOURCE_ENVIRONMENTS: &[&str] = &{json.dumps(list(inputs.contract('automation_skill_sources_v1.json')["environments"]))};',
        f'pub const MAX_AUTOMATION_PROJECT_ROOT_CHARACTERS: usize = {inputs.contract('automation_storage_v1.schema.json')["x-project"]["rootCharacters"]};',
        f'pub const MAX_AUTOMATION_PROJECT_LABEL_CHARACTERS: usize = {inputs.contract('automation_storage_v1.schema.json')["x-project"]["labelCharacters"]};',
        f'pub const MAX_AUTOMATION_EVENT_CLOCK_SKEW_MS: u64 = {inputs.contract('automation_storage_v1.schema.json')["x-limits"]["clockSkewMs"]};',
        f'pub const MAX_AUTOMATION_TRIGGER_CHAIN: usize = {inputs.contract('automation_v1.schema.json')["x-limits"]["triggerChain"]};',
        f'pub const AUTOMATION_ACTION_CATALOG_JSON: &str = {json.dumps(automation_catalog_json(inputs), ensure_ascii=False)};',
        f'pub const AUTOMATION_TERMINAL_RUN_STATES: &[&str] = &{json.dumps(automation_runtime(inputs)["terminalRunStates"])};',
        f'pub const AUTOMATION_ACTIVE_EFFECT_STEP_STATES: &[&str] = &{json.dumps(automation_runtime(inputs)["activeEffectStepStates"])};',
        f'pub const AUTOMATION_CANCELLABLE_PENDING_STEP_STATES: &[&str] = &{json.dumps(automation_runtime(inputs)["cancellablePendingStepStates"])};',
        f'pub const WORKER_JOB_DOCUMENT_NAMESPACE: &str = "{worker_jobs["physicalNamespace"]}";',
        f'pub const WORKER_JOB_IDEMPOTENCY_NAMESPACE: &str = "{worker_jobs["idempotencyNamespace"]}";',
        f'pub const WORKER_JOB_QUEUED_INDEX_NAMESPACE: &str = "{worker_jobs["queuedIndexNamespace"]}";',
        f'pub const WORKER_JOB_QUEUED_SUMMARY_NAMESPACE: &str = "{worker_jobs["queuedSummaryNamespace"]}";',
        f'pub const WORKER_JOB_LEASE_INDEX_NAMESPACE: &str = "{worker_jobs["leaseIndexNamespace"]}";',
        "pub const MAX_WORKER_JOB_PAYLOAD_BYTES: usize = "
        f"{worker_jobs['maximumPayloadBytes']};",
        "pub const MAX_WORKER_JOB_ERROR_BYTES: usize = "
        f"{worker_jobs['maximumErrorBytes']};",
        "pub const MAX_WORKER_JOB_CLAIM_KINDS: usize = "
        f"{worker_jobs['maximumClaimTaskKinds']};",
        "pub const MAX_WORKER_JOB_PRIORITY: usize = "
        f"{worker_jobs['maximumPriority']};",
        "pub const MAX_WORKER_JOB_CLOCK_MILLISECONDS: u64 = "
        f"{worker_jobs['maximumClockMilliseconds']};",
        'pub const MAX_WORKER_JOB_LEASE_NOW_MILLISECONDS: u64 = '
        f'{worker_jobs["maximumClockMilliseconds"] - worker["leaseAuthority"]["maximumLeaseMilliseconds"]};',
        "pub const MAX_WORKER_JOB_FUTURE_CLOCK_SKEW_MILLISECONDS: u64 = "
        f"{worker_jobs['maximumFutureClockSkewMilliseconds']};",
        f'pub const COMPACTION_ARCHIVE_DOCUMENT_NAMESPACE: &str = "{archives["physicalNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_MESSAGES_NAMESPACE: &str = "{archives["messagesNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_POST_MESSAGES_NAMESPACE: &str = "{archives["postMessagesNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_SUMMARY_NAMESPACE: &str = "{archives["summaryNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_RECEIPT_NAMESPACE: &str = "{archives["receiptNamespace"]}";',
        "pub const COMPACTION_ARCHIVE_CONVERSATION_INDEX_NAMESPACE: &str =\n"
        f'    "{archives["conversationIndexNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_COUNT_NAMESPACE: &str = "{archives["conversationCountNamespace"]}";',
        f'pub const COMPACTION_ARCHIVE_ID_CLAIM_NAMESPACE: &str = "{archives["tenantGlobalClaimNamespace"]}";',
        f"pub const MAX_COMPACTION_ARCHIVE_DOCUMENT_BYTES: usize = {archives['maximumDocumentBytes']};",
        "pub const MAX_COMPACTION_ARCHIVES_PER_CONVERSATION: usize = "
        f"{archives['maximumArchivesPerConversation']};",
        f'pub const CONVERSATION_DOCUMENT_NAMESPACE: &str = "{conversations["physicalNamespace"]}";',
        f'pub const CONVERSATION_ID_CLAIM_NAMESPACE: &str = "{conversations["tenantGlobalClaimNamespace"]}";',
        f'pub const CONVERSATION_COUNT_NAMESPACE: &str = "{conversations["ownerCountNamespace"]}";',
        f'pub const CONVERSATION_SEARCH_DIRTY_NAMESPACE: &str = "{conversations["searchDirtyNamespace"]}";',
        f'pub const CONVERSATION_UPDATED_INDEX_NAMESPACE: &str = "{conversations["updatedIndexNamespace"]}";',
        "pub const CONVERSATION_ACTIVITY_CANDIDATE_INDEX_NAMESPACE: &str =\n"
        f'    "{conversations["activityCandidateIndexNamespace"]}";',
        "pub const CONVERSATION_ACTIVITY_CANDIDATE_STATE_NAMESPACE: &str =\n"
        f'    "{conversations["activityCandidateStateNamespace"]}";',
        "pub const MAX_ACTIVITY_CANDIDATE_BACKFILL_ROWS_PER_TRANSACTION: usize = "
        f'{conversations["maximumActivityCandidateBackfillRowsPerTransaction"]};',
        "pub const MAX_ACTIVITY_CANDIDATE_BACKFILL_SOURCE_BYTES_PER_TRANSACTION: usize = "
        f'{conversations["maximumActivityCandidateBackfillSourceBytesPerTransaction"]};',
        f'pub const CONVERSATION_EXECUTION_EPOCH_NAMESPACE: &str = "{conversations["executionEpochNamespace"]}";',
        f'pub const CONVERSATION_TRASH_METADATA_NAMESPACE: &str = "{conversations["trashMetadataNamespace"]}";',
        f'pub const CONVERSATION_TRASH_AGE_INDEX_NAMESPACE: &str = "{conversations["trashAgeIndexNamespace"]}";',
        "pub const CONVERSATION_TRASH_SETTINGS_OVERLAY_NAMESPACE: &str =\n"
        f'    "{conversations["trashSettingsOverlayNamespace"]}";',
        "pub const CONVERSATION_RECENCY_DIRECTORY_NAMESPACE: &str =\n"
        f'    "{conversations["recencyDirectoryNamespace"]}";',
        "pub const CONVERSATION_RECENCY_DIRECTORY_STATE_NAMESPACE: &str =\n"
        f'    "{conversations["recencyDirectoryStateNamespace"]}";',
        "pub const MAX_RECENCY_DIRECTORY_ENTRIES: usize = "
        f'{conversations["maximumRecencyDirectoryEntries"]};',
        "pub const MAX_RECENCY_DIRECTORY_BACKFILL_ROWS_PER_TRANSACTION: usize = "
        f'{conversations["maximumRecencyDirectoryBackfillRowsPerTransaction"]};',
        f'pub const TURN_DOCUMENT_NAMESPACE: &str = "{turns["physicalNamespace"]}";',
        f'pub const TURN_LANE_INDEX_NAMESPACE: &str = "{turns["laneIndexNamespace"]}";',
        f'pub const TURN_ACTIVITY_INDEX_NAMESPACE: &str = "{turns["activityIndexNamespace"]}";',
        "pub const MAX_ACTIVITY_TURN_ROWS_PER_QUERY: usize = "
        f"{turns['maximumActivityTurnRowsPerQuery']};",
        f'pub const TURN_LANE_HEAD_NAMESPACE: &str = "{turns["laneHeadNamespace"]}";',
        "pub const TURN_LANE_LIVE_ATTEMPT_NAMESPACE: &str = "
        f'"{turns["laneLiveAttemptNamespace"]}";',
        f'pub const TURN_LANE_COUNT_NAMESPACE: &str = "{turns["laneCountNamespace"]}";',
        f'pub const TURN_SEARCH_DIRTY_NAMESPACE: &str = "{turns["searchDirtyNamespace"]}";',
        f'pub const CONVERSATION_SYNC_HEAD_NAMESPACE: &str = "{turns["syncHeadNamespace"]}";',
        f'pub const CONVERSATION_SYNC_EVENT_NAMESPACE: &str = "{turns["syncEventNamespace"]}";',
        "pub const CONVERSATION_SYNC_AGE_INDEX_NAMESPACE: &str = "
        f'"{turns["syncAgeIndexNamespace"]}";',
        "pub const ATTEMPT_EVENT_SYNC_REFERENCE_NAMESPACE: &str = "
        f'"{turns["attemptEventSyncReferenceNamespace"]}";',
        "pub const MAX_SYNC_PRUNE_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumSyncPruneRowsPerTransaction"]};',
        "pub const MAX_SYNC_PRUNE_MATERIALIZED_BYTES: usize = "
        f'{turns["maximumSyncPruneMaterializedBytes"]};',
        f'pub const GENERATION_ATTEMPT_NAMESPACE: &str = "{turns["attemptNamespace"]}";',
        "pub const ATTEMPT_DISPATCHABLE_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptDispatchableIndexNamespace"]}";',
        "pub const MAX_DISPATCHABLE_ATTEMPTS_PER_QUERY: usize = "
        f'{turns["maximumDispatchableAttemptsPerQuery"]};',
        "pub const TURN_LANE_COMPACTION_INDEX_NAMESPACE: &str = "
        f'"{turns["laneCompactionIndexNamespace"]}";',
        "pub const MAX_HUMAN_GUIDANCE_RESPONSE_CHARACTERS: usize = "
        f'{turns["humanGuidanceAnswer"]["maximumResponseCharacters"]};',
        "pub const MAX_HUMAN_GUIDANCE_IDENTITY_CHARACTERS: usize = "
        f'{turns["humanGuidanceAnswer"]["maximumIdentityCharacters"]};',
        "pub const MAX_HUMAN_GUIDANCE_PAYLOAD_BYTES: usize = "
        f'{turns["humanGuidanceAnswer"]["maximumPayloadBytes"]};',
        "pub const MAX_TURN_COMPACTION_METADATA_ROWS: usize = "
        f'{turns["maximumCompactionMetadataRows"]};',
        "pub const MAX_TURN_COMPACTION_PROJECTION_UPDATES: usize = "
        f'{turns["maximumCompactionProjectionUpdates"]};',
        "pub const MAX_TURN_COMPACTION_REPARENTED_TURNS: usize = "
        f'{turns["maximumCompactionReparentedTurns"]};',
        "pub const MAX_TURN_COMPACTION_MATERIALIZED_BYTES: usize = "
        f'{turns["maximumCompactionMaterializedBytes"]};',
        "pub const ATTEMPT_RECOVERY_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptRecoveryIndexNamespace"]}";',
        "pub const MAX_RECOVERY_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumRecoveryRowsPerTransaction"]};',
        "pub const MAX_RECOVERY_INDEX_ROWS_PER_OWNER: usize = "
        f'{turns["maximumRecoveryIndexRowsPerOwner"]};',
        "pub const MAX_RECOVERY_PROJECTION_BYTES_PER_TRANSACTION: usize = "
        f'{turns["maximumRecoveryProjectionBytesPerTransaction"]};',
        "pub const ATTEMPT_TIMING_TASK_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptTimingTaskIndexNamespace"]}";',
        "pub const ATTEMPT_TIMING_CONVERSATION_INDEX_NAMESPACE: &str =\n    "
        f'"{turns["attemptTimingConversationIndexNamespace"]}";',
        "pub const MAX_TIMING_TRACE_ROWS_PER_QUERY: usize = "
        f'{turns["maximumTimingTraceRowsPerQuery"]};',
        "pub const MAX_TIMING_TRACE_TASK_CANDIDATES: usize = "
        f'{turns["maximumTimingTraceTaskCandidates"]};',
        "pub const MAX_TIMING_TRACE_CLIENT_OBSERVATIONS: usize = "
        f'{turns["maximumTimingTraceClientObservations"]};',
        "pub const MAX_TIMING_TRACE_PERSISTED_BYTES: usize = "
        f'{turns["maximumTimingTracePersistedBytes"]};',
        "pub const MAX_TIMING_TRACE_COUNTER: u64 = "
        f'{turns["maximumTimingTraceCounter"]};',
        "pub const TURN_PROJECTION_HEAD_NAMESPACE: &str = "
        f'"{turns["projectionHeadNamespace"]}";',
        "pub const TURN_METADATA_SUMMARY_NAMESPACE: &str = "
        f'"{turns["metadataSummaryNamespace"]}";',
        "pub const MAX_TURN_PROJECTION_INLINE_LIVE_BYTES: usize = "
        f'{turns["maximumTurnProjectionInlineLiveBytes"]};',
        "pub const MAX_TURN_PROJECTION_HEAD_PATCHES: usize = "
        f'{turns["maximumTurnProjectionHeadPatches"]};',
        "pub const MAX_TURN_PROJECTION_TEXT_PAGE_BYTES: usize = "
        f'{turns["maximumTurnProjectionTextPageBytes"]};',
        "pub const MAX_TURN_PROJECTION_TEXT_APPEND_BYTES: usize = "
        f'{turns["maximumTurnProjectionTextAppendBytes"]};',
        "pub const MAX_TURN_PROJECTION_PATCH_BYTES: usize = "
        f'{turns["maximumTurnProjectionPatchBytes"]};',
        "pub const MAX_TURN_PROJECTION_PATCH_DEPTH: usize = "
        f'{turns["maximumTurnProjectionPatchDepth"]};',
        "pub const MAX_TURN_PROJECTION_PATCH_OPERATIONS: usize = "
        f'{turns["maximumTurnProjectionPatchOperations"]};',
        "pub const MAX_LEGACY_TURN_IMAGES: usize = "
        f'{turns["maximumLegacyTurnImages"]};',
        "pub const MAX_LEGACY_TURN_IMAGE_BYTES: usize = "
        f'{turns["maximumLegacyTurnImageBytes"]};',
        f'pub const ATTEMPT_COMMAND_NAMESPACE: &str = "{turns["attemptCommandNamespace"]}";',
        f'pub const ATTEMPT_TURN_COUNT_NAMESPACE: &str = "{turns["attemptTurnCountNamespace"]}";',
        f'pub const ATTEMPT_TURN_DIRECTORY_NAMESPACE: &str = "{turns["attemptTurnDirectoryNamespace"]}";',
        "pub const MAX_ATTEMPTS_PER_TURN: usize = "
        f'{turns["maximumAttemptsPerTurn"]};',
        "pub const MAX_ATTEMPT_TURN_DIRECTORY_BYTES: usize = "
        f'{turns["maximumAttemptTurnDirectoryBytes"]};',
        "pub const MAX_ATTEMPT_TOMBSTONE_BYTES: usize = "
        f'{turns["maximumAttemptTombstoneBytes"]};',
        f'pub const ATTEMPT_EVENT_HEAD_NAMESPACE: &str = "{turns["attemptEventHeadNamespace"]}";',
        f'pub const ATTEMPT_EVENT_NAMESPACE: &str = "{turns["attemptEventNamespace"]}";',
        f'pub const MAX_ATTEMPT_EVENT_DOCUMENT_BYTES: usize = {turns["maximumAttemptEventDocumentBytes"]};',
        f'pub const MAX_ATTEMPT_EVENT_PAGE_BYTES: usize = {turns["maximumAttemptEventPageBytes"]};',
        "pub const ATTEMPT_EVENT_RETENTION_INDEX_NAMESPACE: &str = "
        f'"{turns["attemptEventRetentionIndexNamespace"]}";',
        "pub const CONVERSATION_EVENT_RETIREMENT_NAMESPACE: &str = "
        f'"{turns["conversationEventRetirementNamespace"]}";',
        "pub const MAX_ATTEMPT_EVENT_PRUNE_ATTEMPTS_PER_TRANSACTION: usize = "
        f'{turns["maximumAttemptEventPruneAttemptsPerTransaction"]};',
        "pub const MAX_ATTEMPT_EVENT_PRUNE_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumAttemptEventPruneRowsPerTransaction"]};',
        "pub const MAX_ATTEMPT_EVENT_PRUNE_MATERIALIZED_BYTES: usize = "
        f'{turns["maximumAttemptEventPruneMaterializedBytes"]};',
        f'pub const TURN_ID_CLAIM_NAMESPACE: &str = "{turns["turnIdClaimNamespace"]}";',
        f'pub const ATTEMPT_ID_CLAIM_NAMESPACE: &str = "{turns["attemptIdClaimNamespace"]}";',
        "pub const ATTEMPT_CLAIM_LOCATOR_VERSION: u32 = "
        f"{turns['attemptClaimLocatorVersion']};",
        f'pub const TURN_UPDATED_INDEX_NAMESPACE: &str = "{turns["updatedIndexNamespace"]}";',
        f'pub const TURN_TOMBSTONE_NAMESPACE: &str = "{turns["tombstoneNamespace"]}";',
        f'pub const TURN_TOMBSTONE_AGE_INDEX_NAMESPACE: &str = "{turns["tombstoneAgeIndexNamespace"]}";',
        "pub const ATTEMPT_SUPERSEDED_INDEX_NAMESPACE: &str = "
        f'"{turns["supersededAttemptIndexNamespace"]}";',
        "pub const MAX_CLEANUP_ATTEMPTS_PER_TRANSACTION: usize = "
        f'{turns["maximumCleanupAttemptsPerTransaction"]};',
        "pub const MAX_CLEANUP_INDEX_SCAN_ROWS_PER_TRANSACTION: usize = "
        f'{turns["maximumCleanupIndexScanRowsPerTransaction"]};',
        "pub const MAX_DESKTOP_AGENT_ID_CHARACTERS: usize = "
        f"{agent_field['maximumCharacters']};",
        "pub static EXECUTABLE_STORAGE_OPERATIONS: &[&str] = &[",
    ]
