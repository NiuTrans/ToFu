"""Project mutable-field vocabulary and migration constants.
Entry: render_migration_suffix. Appends the original ordered native suffix; no reads or writes."""

import json


def render_migration_suffix(document) -> list[str]:
    lines = []
    lines.extend(
        f'    "{operation["name"]}",' for operation in document["executableOperations"]
    )
    lines.extend(["];"])
    trash = document["conversationTrashMigration"]
    lines.append(f'pub const MIGRATION_TRASH_STAGE_NAMESPACE: &str = "{trash["stageNamespace"]}";')
    lines.append(f'pub const MAX_MIGRATION_TRASH_TURNS: usize = {trash["maximumTurnsPerCapsule"]};')
    lines.append(f'pub const MAX_MIGRATION_TRASH_STAGE_BYTES: usize = {trash["maximumStageBytes"]};')
    stores = document["migrationStores"]
    lines.append("pub const BROWSER_OBSERVATION_OWNER_TOUCH_NAMESPACE: &str =\n"
                 f'    "{stores["browserObservationOwnerTouchNamespace"]}";')
    lines.append("pub const PLUGIN_ROW_DOCUMENT_NAMESPACE: &str = "
                 f'"{stores["pluginRowDocumentNamespace"]}";')
    lines.append("pub const PLUGIN_ROW_UNIQUE_NAMESPACE: &str = "
                 f'"{stores["pluginRowUniqueNamespace"]}";')
    lines.append("pub const MAX_PLUGIN_ROW_DOCUMENT_BYTES: usize = "
                 f'{stores["maximumPluginRowDocumentBytes"]};')
    lines.append("pub const MAX_PLUGIN_ROW_FIELDS: usize = "
                 f'{stores["maximumPluginRowFields"]};')
    lines.append("pub const MAX_PLUGIN_UNIQUE_VALUES_PER_ROW: usize = "
                 f'{stores["maximumPluginUniqueValuesPerRow"]};')
    lines.append("pub const MAX_MIGRATION_DOCUMENT_BYTES: usize = "
                 f'{document["offlineMigration"]["documentImport"]["maximumJSONBytes"]};')
    lines.append("pub const MAX_MIGRATION_TASK_RESULT_DOCUMENT_BYTES: usize = "
                 f'{document["offlineMigration"]["documentImport"]["taskResultMaximumJSONBytes"]};')
    operator = document["offlineMigration"]["operatorSession"]
    lines.append(f'pub const MIGRATION_SESSION_FORMAT: &str = "{operator["format"]}";')
    lines.append(f'pub const MIGRATION_SESSION_MAX_FRAME_BYTES: usize = {operator["maximumFrameBytes"]};')
    lines.append(f'pub const MIGRATION_SESSION_MAX_PAGE_ROWS: usize = {operator["maximumCatalogPageRows"]};')
    lines.append(f'pub const MAX_FROZEN_TRANSCRIPT_SOURCE_BYTES: usize = {document["offlineMigration"]["maximumFrozenTranscriptSourceBytes"]};')
    for name, field in [("MAX_TURN_SNAPSHOT_SOURCE_BYTES", "maximumSourceBytes"),
                        ("MAX_TURN_SNAPSHOT_BYTES", "maximumSnapshotBytes"),
                        ("MAX_TURN_RESPONSE_BYTES", "maximumResponseBytes")]:
        lines.append(f'pub const {name}: usize = {document["turnSnapshots"][field]};')
    frozen = document["frozenTranscripts"]
    for name, field in [("FROZEN_TRANSCRIPT_MANIFEST_NAMESPACE", "manifestNamespace"),
                        ("FROZEN_TRANSCRIPT_PAGE_NAMESPACE", "pageNamespace")]:
        lines.append(f'pub const {name}: &str = "{frozen[field]}";')
    archive = document["transcriptArchives"]
    for name, field in [("TRANSCRIPT_ARCHIVE_FORMAT", "format"),
                        ("TRANSCRIPT_ARCHIVE_RETIRED_FORMAT", "retiredFormat"),
                        ("TRANSCRIPT_ARCHIVE_MANIFEST_NAMESPACE", "manifestNamespace"),
                        ("TRANSCRIPT_ARCHIVE_PAGE_NAMESPACE", "pageNamespace")]:
        lines.append(f'pub const {name}: &str = "{archive[field]}";')
    for name, field in [("MAX_TRANSCRIPT_ARCHIVE_PAGES", "maximumPages"),
                        ("MAX_TRANSCRIPT_ARCHIVE_MESSAGES", "maximumMessages"),
                        ("TRANSCRIPT_ARCHIVE_TAIL_TARGET_BYTES", "tailTargetBytes"),
                        ("MAX_TRANSCRIPT_ARCHIVE_DECODED_BYTES", "maximumDecodedBytes")]:
        lines.append(f'pub const {name}: usize = {archive[field]};')
    for name, field in [("MAX_FROZEN_TRANSCRIPT_PAGES", "maximumPages"),
                        ("MAX_FROZEN_TRANSCRIPT_MESSAGES", "maximumMessages"),
                        ("MAX_FROZEN_TRANSCRIPT_PAGE_BYTES", "maximumPageBytes"),
                        ("MAX_FROZEN_TRANSCRIPT_DECODED_BYTES", "maximumDecodedBytes")]:
        lines.append(f'pub const {name}: usize = {frozen[field]};')
    lines.append(f'pub const MIGRATION_SESSION_MAX_SOURCE_CHUNK_BYTES: usize = {document["offlineMigration"]["maximumSourceChunkBytes"]};')
    lines.append(f'pub const MIGRATION_SESSION_MAX_PAGE_BYTES: usize = {operator["maximumCatalogPageBytes"]};')
    legacy_receipts = document["offlineMigration"]["legacyReceiptImport"]
    lines.append(f'pub const LEGACY_RECEIPT_OWNER_ID: u64 = {legacy_receipts["reservedOwnerId"]};')
    lines.append(f'pub const MAX_LEGACY_RECEIPT_ROWS: u64 = {legacy_receipts["maximumRows"]};')
    lines.append(f'pub const MAX_LEGACY_RECEIPT_STORED_BYTES: u64 = {legacy_receipts["maximumStoredBytes"]};')
    for name, key in [("MIGRATION_SESSION_SEMANTIC_OPERATIONS", "semanticOperations"),
                      ("MIGRATION_SESSION_EXCLUDED_NAMESPACES", "excludedRecordNamespaces"),
                      ("MIGRATION_SESSION_BILLING_OPERATIONS", "billingOperations"),
                      ("MIGRATION_SESSION_COMMAND_RECEIPT_OPERATIONS", "commandReceiptOperations"),
                      ("MIGRATION_SESSION_TENANT_USER_OPERATIONS", "tenantUserOperations"),
                      ("MIGRATION_SESSION_TASK_RESULT_OPERATIONS", "taskResultOperations"),
                      ("MIGRATION_SESSION_TURN_OPERATIONS", "turnOperations"),
                      ("MIGRATION_SESSION_CONVERSATION_OPERATIONS", "conversationOperations"),
                      ("MIGRATION_SESSION_TRASH_OPERATIONS", "trashOperations"),
                      ("MIGRATION_SESSION_ATTEMPT_OPERATIONS", "attemptOperations"),
                      ("MIGRATION_SESSION_ATTEMPT_EVENT_OPERATIONS", "attemptEventOperations"),
                      ("MIGRATION_SESSION_ATTEMPT_EVENT_UPLOAD_OPERATIONS", "attemptEventUploadOperations"),
                      ("MIGRATION_SESSION_STORAGE_EVENT_OPERATIONS", "storageEventOperations"),
                      ("MIGRATION_SESSION_CONVERSATION_SYNC_EVENT_OPERATIONS", "conversationSyncEventOperations"),
                      ("MIGRATION_SESSION_CONVERSATION_SYNC_HEAD_OPERATIONS", "conversationSyncHeadOperations"),
                      ("MIGRATION_SESSION_CONTINUATION_OPERATIONS", "continuationOperations"),
                      ("MIGRATION_SESSION_PAPER_BUILD_BUDGET_OPERATIONS", "paperBuildBudgetOperations"),
                      ("MIGRATION_SESSION_PAPER_DOCUMENT_BUDGET_OPERATIONS", "paperDocumentBudgetOperations"),
                      ("MIGRATION_SESSION_DESKTOP_EGRESS_OPERATIONS", "desktopEgressOperations"),
                      ("MIGRATION_SESSION_DAILY_COST_OPERATIONS", "dailyCostOperations"),
                      ("MIGRATION_SESSION_PAPER_LIBRARY_OPERATIONS", "paperLibraryOperations"),
                      ("MIGRATION_SESSION_PAPER_REPORT_OPERATIONS", "paperReportOperations"),
                      ("MIGRATION_SESSION_PAPER_TRANSLATION_OPERATIONS", "paperTranslationOperations"),
                      ("MIGRATION_SESSION_PAPER_NOTE_OPERATIONS", "paperNoteOperations"),
                      ("MIGRATION_SESSION_PAPER_PODCAST_OPERATIONS", "paperPodcastOperations"),
                      ("MIGRATION_SESSION_KNOWLEDGE_OPERATIONS", "knowledgeOperations"),
                      ("MIGRATION_SESSION_CHAT_ARTIFACT_OPERATIONS", "chatArtifactOperations"),
                      ("MIGRATION_SESSION_TOOL_RESULT_OPERATIONS", "toolResultOperations"),
                      ("MIGRATION_SESSION_BROWSER_OBSERVATION_OPERATIONS", "browserObservationOperations"),
                      ("MIGRATION_SESSION_MODEL_ROUTING_AUTHORITY_OPERATIONS", "modelRoutingAuthorityOperations"),
                      ("MIGRATION_SESSION_MODEL_ROUTING_SECRET_OPERATIONS", "modelRoutingSecretOperations"),
                      ("MIGRATION_SESSION_PLUGIN_MANIFEST_OPERATIONS", "pluginManifestOperations"),
                      ("MIGRATION_SESSION_PLUGIN_ROWS_OPERATIONS", "pluginRowsOperations"),
                      ("MIGRATION_SESSION_PLUGIN_UNIQUE_OPERATIONS", "pluginUniqueOperations"),
                      ("MIGRATION_SESSION_PROJECT_BRAIN_OPERATIONS", "projectBrainOperations"),
                      ("MIGRATION_SESSION_OPTIMIZER_OPERATIONS", "optimizerOperations"),
                      ("MIGRATION_SESSION_LOG_AGGREGATE_OPERATIONS", "logAggregateOperations"),
                      ("MIGRATION_SESSION_BILLING_PAYMENT_OPERATIONS", "billingPaymentOperations"),
                      ("MIGRATION_SESSION_BILLING_REDEEM_OPERATIONS", "billingRedeemOperations"),
                      ("MIGRATION_SESSION_RATE_LIMIT_OPERATIONS", "rateLimitEventOperations"),
                      ("MIGRATION_SESSION_ORCHESTRATION_DEFINITION_OPERATIONS", "orchestrationDefinitionOperations"),
                      ("MIGRATION_SESSION_ORCHESTRATION_RUN_OPERATIONS", "orchestrationRunOperations"),
                      ("MIGRATION_SESSION_ORCHESTRATION_EVENT_OPERATIONS", "orchestrationEventOperations"),
                      ("MIGRATION_SESSION_SWARM_SESSION_OPERATIONS", "swarmSessionOperations"),
                      ("MIGRATION_SESSION_SWARM_AGENT_OPERATIONS", "swarmAgentOperations"),
                      ("MIGRATION_SESSION_INTEGRATION_WORKSPACE_OPERATIONS", "integrationWorkspaceOperations"),
                      ("MIGRATION_SESSION_INTEGRATION_EVENT_OPERATIONS", "integrationEventOperations"),
                      ("MIGRATION_SESSION_INTEGRATION_META_OPERATIONS", "integrationMetaOperations"),
                      ("MIGRATION_SESSION_QUEUE_ITEM_OPERATIONS", "queueItemOperations"),
                      ("MIGRATION_SESSION_QUEUE_AUTOPILOT_MARKER_OPERATIONS", "queueAutopilotMarkerOperations"),
                      ("MIGRATION_SESSION_TIMER_OPERATIONS", "timerOperations"),
                      ("MIGRATION_SESSION_TIMER_POLL_OPERATIONS", "timerPollOperations"),
                      ("MIGRATION_SESSION_SCHEDULER_TASK_OPERATIONS", "schedulerTaskOperations"),
                      ("MIGRATION_SESSION_SCHEDULER_POLL_OPERATIONS", "schedulerPollOperations"),
                      ("MIGRATION_SESSION_WORKER_JOB_OPERATIONS", "workerJobOperations"),
                      ("MIGRATION_SESSION_FROZEN_TRANSCRIPT_OPERATIONS", "frozenTranscriptOperations"),
                      ("MIGRATION_SESSION_FROZEN_TRANSCRIPT_UPLOAD_OPERATIONS", "frozenTranscriptUploadOperations"),
                      ("MIGRATION_SESSION_RAW_ARCHIVE_OPERATIONS", "rawArchiveOperations"),
                      ("MIGRATION_SESSION_COMPACTION_ARCHIVE_OPERATIONS", "compactionArchiveOperations"),
                      ("MIGRATION_SESSION_EXCLUDED_PREFIXES", "excludedRecordPrefixes")]:
        prefix = f'pub const {name}: &[&str] ='
        literal = f'&{json.dumps(operator[key])};'
        if len(prefix) + 1 + len(literal) <= 100:
            lines.append(f'{prefix} {literal}')
        elif len(operator[key]) == 1 and len(literal) <= 96:
            # rustfmt-canonical two-line layout for a short array literal:
            # `cargo fmt --check` and this generator's exact-text --check
            # must agree.
            lines.append(prefix)
            lines.append(f'    {literal}')
        else:
            # rustfmt-canonical multi-line array layout: `cargo fmt --check`
            # and this generator's exact-text --check must agree.
            lines.append(f'{prefix} &[')
            lines.extend(f'    "{operation}",' for operation in operator[key])
            lines.append('];')
    return lines
