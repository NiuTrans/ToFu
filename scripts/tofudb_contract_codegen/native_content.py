"""Project content constants into the native IR output.
Entry: render_content. Pure ordered projection of validated IR and shared input documents."""




def render_content(tables) -> list[str]:
    providers = tables["byo_providers"]
    artifacts = tables["chat_artifacts"]
    tool_result_artifacts = tables["tool_result_artifacts"]
    log_aggregates = tables["log_aggregates"]
    optimizer_actions = tables["optimizer_actions"]
    optimizer_proposals = tables["optimizer_proposals"]
    paper_library = tables["paper_library"]
    paper_notes = tables["paper_notes"]
    plugin_manifests = tables["plugin_manifests"]
    knowledge_documents = tables["knowledge_documents"]
    paper_reports = tables["paper_reports"]
    paper_translations = tables["paper_translations"]
    paper_podcasts = tables["paper_podcasts"]
    raw_archives = tables["raw_archives"]
    research_artifacts = tables["research_artifacts"]
    research_workspaces = tables["research_workspaces"]
    model_routing = tables["model_routing"]
    return [
        f'pub const ARTIFACT_DOCUMENT_NAMESPACE: &str = "{artifacts["physicalNamespace"]}";',
        f'pub const ARTIFACT_CONVERSATION_INDEX_NAMESPACE: &str = "{artifacts["conversationIndexNamespace"]}";',
        f'pub const ARTIFACT_LIBRARY_INDEX_NAMESPACE: &str = "{artifacts["libraryIndexNamespace"]}";',
        f'pub const ARTIFACT_DEDUPE_NAMESPACE: &str = "{artifacts["dedupeNamespace"]}";',
        f'pub const ARTIFACT_PATH_HEAD_NAMESPACE: &str = "{artifacts["pathHeadNamespace"]}";',
        f'pub const ARTIFACT_PARENT_INDEX_NAMESPACE: &str = "{artifacts["parentIndexNamespace"]}";',
        f'pub const TOOL_RESULT_ARTIFACT_NAMESPACE: &str = "{tool_result_artifacts["physicalNamespace"]}";',
        f'pub const TOOL_RESULT_EXPIRY_INDEX_NAMESPACE: &str = "{tool_result_artifacts["expiryIndexNamespace"]}";',
        f'pub const TOOL_RESULT_CURSOR_KEY_NAMESPACE: &str = "{tool_result_artifacts["cursorKeyNamespace"]}";',
        f'pub const MAX_TOOL_RESULT_ARTIFACT_BYTES: usize = {tool_result_artifacts["maximumContentBytes"]};',
        f'pub const MAX_TOOL_RESULT_RANGE_BYTES: usize = {tool_result_artifacts["maximumRangeBytes"]};',
        f'pub const MAX_TOOL_RESULT_TTL_MILLISECONDS: u64 = {tool_result_artifacts["maximumTtlMilliseconds"]};',
        f'pub const MAX_TOOL_RESULT_PRUNE_ROWS: usize = {tool_result_artifacts["maximumPruneRows"]};',
        f'pub const LOG_AGGREGATE_DOCUMENT_NAMESPACE: &str = "{log_aggregates["physicalNamespace"]}";',
        f'pub const LOG_AGGREGATE_SWEEP_INDEX_NAMESPACE: &str = "{log_aggregates["sweepIndexNamespace"]}";',
        f'pub const LOG_AGGREGATE_SEQUENCE_NAMESPACE: &str = "{log_aggregates["sequenceNamespace"]}";',
        f'pub const MAX_LOG_AGGREGATE_DOCUMENT_BYTES: usize = {log_aggregates["maximumDocumentBytes"]};',
        f'pub const MAX_LOG_AGGREGATE_FINGERPRINT_CHARACTERS: usize = {log_aggregates["maximumFingerprintCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_LEVEL_CHARACTERS: usize = {log_aggregates["maximumLevelCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_LOGGER_CHARACTERS: usize = {log_aggregates["maximumLoggerCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_TEMPLATE_CHARACTERS: usize = {log_aggregates["maximumTemplateCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_SAMPLE_CHARACTERS: usize = {log_aggregates["maximumSampleCharacters"]};',
        f'pub const MAX_LOG_AGGREGATE_FLUSH_BATCH: usize = {log_aggregates["maximumFlushBatch"]};',
        f'pub const MAX_LOG_AGGREGATE_QUERY_ROWS: usize = {log_aggregates["maximumQueryRows"]};',
        f'pub const MAX_LOG_AGGREGATE_SCAN_ROWS: usize = {log_aggregates["maximumScanRows"]};',
        f'pub const MAX_LOG_AGGREGATE_SWEEP_BATCH: usize = {log_aggregates["maximumSweepBatch"]};',
        f'pub const MAX_LOG_AGGREGATE_ROW_COUNT: u64 = {log_aggregates["maximumRowCount"]};',
        f'pub const PAPER_LIBRARY_CORE_NAMESPACE: &str = "{paper_library["coreNamespace"]}";',
        f'pub const PAPER_LIBRARY_STATE_NAMESPACE: &str = "{paper_library["stateNamespace"]}";',
        f'pub const PAPER_LIBRARY_UPDATED_INDEX_NAMESPACE: &str = "{paper_library["updatedIndexNamespace"]}";',
        f'pub const PAPER_LIBRARY_HASH_INDEX_NAMESPACE: &str = "{paper_library["hashIndexNamespace"]}";',
        f'pub const PAPER_LIBRARY_ARXIV_INDEX_NAMESPACE: &str = "{paper_library["arxivIndexNamespace"]}";',
        f'pub const PAPER_LIBRARY_REPORT_PRESENCE_NAMESPACE: &str = "{paper_library["reportPresenceNamespace"]}";',
        f'pub const PAPER_LIBRARY_COUNT_NAMESPACE: &str = "{paper_library["countNamespace"]}";',
        f'pub const MAX_PAPER_LIBRARY_ROWS_PER_OWNER: usize = {paper_library["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_LIBRARY_CORE_DOCUMENT_BYTES: usize = {paper_library["maximumCoreDocumentBytes"]};',
        f'pub const MAX_PAPER_LIBRARY_STATE_DOCUMENT_BYTES: usize = {paper_library["maximumStateDocumentBytes"]};',
        f'pub const MAX_PAPER_LIBRARY_RESPONSE_BYTES: usize = {paper_library["maximumResponseBytes"]};',
        f'pub const MAX_PAPER_LIBRARY_FANIN_PAPERS: usize = {paper_library["maximumFaninPapers"]};',
        f'pub const MAX_PAPER_LIBRARY_FANIN_TEXT_CHARACTERS: usize = {paper_library["maximumFaninTextCharacters"]};',
        f'pub const MAX_PAPER_LIBRARY_QA_TEXT_CHARACTERS: usize = {paper_library["maximumQaTextCharacters"]};',
        f'pub const PAPER_NOTE_DOCUMENT_NAMESPACE: &str = "{paper_notes["physicalNamespace"]}";',
        f'pub const PAPER_NOTE_LIST_INDEX_NAMESPACE: &str = "{paper_notes["listIndexNamespace"]}";',
        f'pub const PAPER_NOTE_COUNT_NAMESPACE: &str = "{paper_notes["countNamespace"]}";',
        f'pub const MAX_PAPER_NOTES_PER_OWNER: usize = {paper_notes["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_NOTE_DOCUMENT_BYTES: usize = {paper_notes["maximumDocumentBytes"]};',
        f'pub const MAX_PAPER_NOTE_RESPONSE_BYTES: usize = {paper_notes["maximumResponseBytes"]};',
        f'pub const PLUGIN_MANIFEST_DOCUMENT_NAMESPACE: &str = "{plugin_manifests["physicalNamespace"]}";',
        "pub const MAX_PLUGIN_NAMESPACE_CHARACTERS: usize = "
        f"{plugin_manifests['maximumNamespaceCharacters']};",
        "pub const MAX_PLUGIN_MANIFEST_BYTES: usize = "
        f"{plugin_manifests['maximumManifestBytes']};",
        f'pub const KNOWLEDGE_DOCUMENT_NAMESPACE: &str = "{knowledge_documents["documentNamespace"]}";',
        f'pub const KNOWLEDGE_CHUNK_DOCUMENT_NAMESPACE: &str = "{knowledge_documents["chunkNamespace"]}";',
        f'pub const KNOWLEDGE_ASSET_DOCUMENT_NAMESPACE: &str = "{knowledge_documents["assetNamespace"]}";',
        f'pub const KNOWLEDGE_ASSET_ORDINAL_INDEX_NAMESPACE: &str = "{knowledge_documents["assetOrdinalIndexNamespace"]}";',
        f'pub const KNOWLEDGE_ASSET_CHUNK_REVERSE_NAMESPACE: &str = "{knowledge_documents["assetChunkReverseNamespace"]}";',
        f'pub const KNOWLEDGE_TERM_NAMESPACE: &str = "{knowledge_documents["termNamespace"]}";',
        f'pub const KNOWLEDGE_DIGEST_INDEX_NAMESPACE: &str = "{knowledge_documents["digestIndexNamespace"]}";',
        f'pub const KNOWLEDGE_SETTINGS_NAMESPACE: &str = "{knowledge_documents["settingsNamespace"]}";',
        f'pub const KNOWLEDGE_CHUNK_ASSET_LINK_NAMESPACE: &str = "{knowledge_documents["chunkAssetLinkNamespace"]}";',
        f'pub const KNOWLEDGE_ENRICHMENT_QUEUE_NAMESPACE: &str = "{knowledge_documents["enrichmentQueueNamespace"]}";',
        f'pub const KNOWLEDGE_ENRICHMENT_OWNER_INDEX_NAMESPACE: &str = "{knowledge_documents["enrichmentOwnerIndexNamespace"]}";',
        "pub const MAX_KNOWLEDGE_DOCUMENT_ID_CHARACTERS: usize = "
        f"{knowledge_documents['maximumDocumentIdCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_ID_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetIdCharacters']};",
        "pub const MAX_KNOWLEDGE_NAME_CHARACTERS: usize = "
        f"{knowledge_documents['maximumNameCharacters']};",
        "pub const MAX_KNOWLEDGE_STORED_NAME_CHARACTERS: usize = "
        f"{knowledge_documents['maximumStoredNameCharacters']};",
        "pub const MAX_KNOWLEDGE_DOCUMENT_KIND_CHARACTERS: usize = "
        f"{knowledge_documents['maximumDocumentKindCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_KIND_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetKindCharacters']};",
        "pub const MAX_KNOWLEDGE_MIME_TYPE_CHARACTERS: usize = "
        f"{knowledge_documents['maximumMimeTypeCharacters']};",
        "pub const MAX_KNOWLEDGE_METHOD_CHARACTERS: usize = "
        f"{knowledge_documents['maximumMethodCharacters']};",
        "pub const MAX_KNOWLEDGE_RELATION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumRelationCharacters']};",
        "pub const MAX_KNOWLEDGE_STATUS_CHARACTERS: usize = "
        f"{knowledge_documents['maximumStatusCharacters']};",
        "pub const MAX_KNOWLEDGE_SCOPE_CHARACTERS: usize = "
        f"{knowledge_documents['maximumScopeCharacters']};",
        "pub const MAX_KNOWLEDGE_SECTION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumSectionCharacters']};",
        "pub const MAX_KNOWLEDGE_LOCATION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumLocationCharacters']};",
        "pub const MAX_KNOWLEDGE_CHUNK_CONTENT_CHARACTERS: usize = "
        f"{knowledge_documents['maximumChunkContentCharacters']};",
        "pub const MAX_KNOWLEDGE_SEARCH_TEXT_CHARACTERS: usize = "
        f"{knowledge_documents['maximumSearchTextCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_TEXT_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetTextCharacters']};",
        "pub const MAX_KNOWLEDGE_CAPTION_CHARACTERS: usize = "
        f"{knowledge_documents['maximumCaptionCharacters']};",
        "pub const MAX_KNOWLEDGE_ENRICHMENT_MODEL_CHARACTERS: usize = "
        f"{knowledge_documents['maximumEnrichmentModelCharacters']};",
        "pub const MAX_KNOWLEDGE_ENRICHMENT_ERROR_CHARACTERS: usize = "
        f"{knowledge_documents['maximumEnrichmentErrorCharacters']};",
        "pub const MAX_KNOWLEDGE_MEDIA_METADATA_CHARACTERS: usize = "
        f"{knowledge_documents['maximumMediaMetadataCharacters']};",
        "pub const MAX_KNOWLEDGE_ASSET_METADATA_CHARACTERS: usize = "
        f"{knowledge_documents['maximumAssetMetadataCharacters']};",
        "pub const MAX_KNOWLEDGE_JSON_ARRAY_CHARACTERS: usize = "
        f"{knowledge_documents['maximumJsonArrayCharacters']};",
        "pub const MAX_KNOWLEDGE_TERM_CHARACTERS: usize = "
        f"{knowledge_documents['maximumTermCharacters']};",
        "pub const MAX_KNOWLEDGE_SEARCH_TOKENS: usize = "
        f"{knowledge_documents['maximumSearchTokens']};",
        "pub const MAX_KNOWLEDGE_PAGE_ROWS: usize = "
        f"{knowledge_documents['maximumPageRows']};",
        "pub const MAX_KNOWLEDGE_CATALOG_PAGE_SIZE: usize = "
        f"{knowledge_documents['maximumCatalogPageSize']};",
        "pub const MAX_KNOWLEDGE_CATALOG_QUERY_CHARACTERS: usize = "
        f"{knowledge_documents['maximumCatalogQueryCharacters']};",
        "pub const MAX_KNOWLEDGE_CHUNKS_PER_DOCUMENT: usize = "
        f"{knowledge_documents['maximumChunksPerDocument']};",
        "pub const MAX_KNOWLEDGE_ASSETS_PER_DOCUMENT: usize = "
        f"{knowledge_documents['maximumAssetsPerDocument']};",
        "pub const MAX_KNOWLEDGE_OWNER_ASSET_ROWS: usize = "
        f"{knowledge_documents['maximumOwnerAssetRows']};",
        "pub const MAX_KNOWLEDGE_ENRICHMENT_OWNER_ROWS: usize = "
        f"{knowledge_documents['maximumEnrichmentOwnerRows']};",
        "pub const MAX_KNOWLEDGE_DOCUMENT_BYTES: usize = "
        f"{knowledge_documents['maximumDocumentBytes']};",
        "pub const MAX_KNOWLEDGE_CHUNK_DOCUMENT_BYTES: usize = "
        f"{knowledge_documents['maximumChunkDocumentBytes']};",
        "pub const MAX_KNOWLEDGE_ASSET_DOCUMENT_BYTES: usize = "
        f"{knowledge_documents['maximumAssetDocumentBytes']};",
        "pub const MAX_KNOWLEDGE_SEARCH_POSTINGS_SCAN: usize = "
        f"{knowledge_documents['maximumSearchPostingsScan']};",
        "pub const MAX_KNOWLEDGE_CATALOG_SCAN_DOCUMENTS: usize = "
        f"{knowledge_documents['maximumCatalogScanDocuments']};",
        "pub const MAX_KNOWLEDGE_CLAIM_SCAN_ROWS: usize = "
        f"{knowledge_documents['maximumClaimScanRows']};",
        f'pub const PAPER_REPORT_CORE_NAMESPACE: &str = "{paper_reports["coreNamespace"]}";',
        f'pub const PAPER_REPORT_META_NAMESPACE: &str = "{paper_reports["metaNamespace"]}";',
        f'pub const PAPER_REPORT_LATEST_INDEX_NAMESPACE: &str = "{paper_reports["latestIndexNamespace"]}";',
        f'pub const PAPER_REPORT_PRESENCE_NAMESPACE: &str = "{paper_reports["presenceNamespace"]}";',
        f'pub const PAPER_REPORT_COUNT_NAMESPACE: &str = "{paper_reports["countNamespace"]}";',
        f'pub const MAX_PAPER_REPORT_ROWS_PER_OWNER: usize = {paper_reports["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_REPORT_DOCUMENT_BYTES: usize = {paper_reports["maximumDocumentBytes"]};',
        f'pub const MAX_PAPER_REPORT_META_BYTES: usize = {paper_reports["maximumMetaBytes"]};',
        f'pub const MAX_PAPER_REPORT_RESPONSE_BYTES: usize = {paper_reports["maximumResponseBytes"]};',
        f'pub const MAX_PAPER_REPORT_REOPEN_SIBLINGS: usize = {paper_reports["maximumReopenSiblings"]};',
        f'pub const MAX_PAPER_REPORT_EXCERPT_HASHES: usize = {paper_reports["maximumExcerptHashes"]};',
        f'pub const MAX_PAPER_REPORT_EXCERPT_TEXT_CHARACTERS: usize = {paper_reports["maximumExcerptTextCharacters"]};',
        f'pub const PAPER_TRANSLATION_DOCUMENT_NAMESPACE: &str = "{paper_translations["documentNamespace"]}";',
        f'pub const PAPER_TRANSLATION_COUNT_NAMESPACE: &str = "{paper_translations["countNamespace"]}";',
        f'pub const MAX_PAPER_TRANSLATION_ROWS_PER_OWNER: usize = {paper_translations["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_TRANSLATION_DOCUMENT_BYTES: usize = {paper_translations["maximumDocumentBytes"]};',
        f'pub const MAX_PAPER_TRANSLATION_RESPONSE_BYTES: usize = {paper_translations["maximumResponseBytes"]};',
        f'pub const PAPER_PODCAST_CORE_NAMESPACE: &str = "{paper_podcasts["coreNamespace"]}";',
        f'pub const PAPER_PODCAST_STATE_NAMESPACE: &str = "{paper_podcasts["stateNamespace"]}";',
        f'pub const PAPER_PODCAST_COUNT_NAMESPACE: &str = "{paper_podcasts["countNamespace"]}";',
        f'pub const PAPER_PODCAST_ACTIVE_INDEX_NAMESPACE: &str = "{paper_podcasts["activeIndexNamespace"]}";',
        f'pub const PAPER_PODCAST_ACTIVE_COUNT_NAMESPACE: &str = "{paper_podcasts["activeCountNamespace"]}";',
        f'pub const MAX_PAPER_PODCAST_ROWS_PER_OWNER: usize = {paper_podcasts["maximumRowsPerOwner"]};',
        f'pub const MAX_PAPER_PODCAST_ACTIVE_PER_TENANT: usize = {paper_podcasts["maximumActivePerTenant"]};',
        f'pub const MAX_PAPER_PODCAST_SCRIPT_BYTES: usize = {paper_podcasts["maximumScriptBytes"]};',
        f'pub const MAX_PAPER_PODCAST_META_BYTES: usize = {paper_podcasts["maximumMetaBytes"]};',
        f'pub const MAX_PAPER_PODCAST_CORE_DOCUMENT_BYTES: usize = {paper_podcasts["maximumCoreDocumentBytes"]};',
        f'pub const MAX_PAPER_PODCAST_STATE_DOCUMENT_BYTES: usize = {paper_podcasts["maximumStateDocumentBytes"]};',
        f'pub const MAX_PAPER_PODCAST_RESPONSE_BYTES: usize = {paper_podcasts["maximumResponseBytes"]};',
        f'pub const RAW_ARCHIVE_DOCUMENT_NAMESPACE: &str = "{raw_archives["physicalNamespace"]}";',
        f'pub const RAW_ARCHIVE_ID_CLAIM_NAMESPACE: &str = "{raw_archives["idClaimNamespace"]}";',
        f'pub const RAW_ARCHIVE_TENANT_USAGE_NAMESPACE: &str = "{raw_archives["tenantUsageNamespace"]}";',
        f'pub const RAW_ARCHIVE_ATTEMPT_USAGE_NAMESPACE: &str = "{raw_archives["attemptUsageNamespace"]}";',
        f'pub const RAW_ARCHIVE_OWNER_COUNT_NAMESPACE: &str = "{raw_archives["ownerCountNamespace"]}";',
        f'pub const RAW_ARCHIVE_TASK_INDEX_NAMESPACE: &str = "{raw_archives["taskIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_TASK_ROUND_INDEX_NAMESPACE: &str = "{raw_archives["taskRoundIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_ATTEMPT_INDEX_NAMESPACE: &str = "{raw_archives["attemptIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_CONVERSATION_INDEX_NAMESPACE: &str = "{raw_archives["conversationIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_CONVERSATION_STATS_NAMESPACE: &str = "{raw_archives["conversationStatsNamespace"]}";',
        f'pub const RAW_ARCHIVE_OWNER_AGE_INDEX_NAMESPACE: &str = "{raw_archives["ownerAgeIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_OWNER_BODY_AGE_INDEX_NAMESPACE: &str = "{raw_archives["ownerBodyAgeIndexNamespace"]}";',
        f'pub const RAW_ARCHIVE_OWNER_AGE_BACKFILL_NAMESPACE: &str = "{raw_archives["ownerAgeBackfillNamespace"]}";',
        "pub const MAX_RAW_ARCHIVE_STORED_BYTES: usize = "
        f"{raw_archives['maximumStoredBytes']};",
        "pub const MAX_RAW_ARCHIVE_DOCUMENT_BYTES: usize = "
        f"{raw_archives['maximumDocumentBytes']};",
        "pub const MAX_RAW_ARCHIVE_DECODED_PART_BYTES: usize = "
        f"{raw_archives['maximumDecodedPartBytes']};",
        "pub const MAX_RAW_ARCHIVE_TENANT_BUDGET_BYTES: u64 = "
        f"{raw_archives['maximumTenantBudgetBytes']};",
        "pub const MAX_RAW_ARCHIVE_READ_CHUNK_BYTES: usize = "
        f"{raw_archives['maximumReadChunkBytes']};",
        "pub const MAX_RAW_ARCHIVES_PER_OWNER: u64 = "
        f"{raw_archives['maximumArchivesPerOwner']};",
        "pub const MAX_RAW_ARCHIVES_PER_CONVERSATION: usize = "
        f"{raw_archives['maximumArchivesPerConversation']};",
        "pub const MAX_RAW_ARCHIVE_SUMMARY_BYTES: usize = "
        f"{raw_archives['maximumSummaryBytes']};",
        "pub const MAX_RAW_ARCHIVE_SUMMARY_KEYS: usize = "
        f"{raw_archives['maximumSummaryKeys']};",
        "pub const MAX_RAW_ARCHIVE_LIST_ROWS: usize = "
        f"{raw_archives['maximumListRows']};",
        "pub const MAX_RAW_ARCHIVES_RECLAIMED_PER_PUT: usize = "
        f"{raw_archives['maximumReclaimedArchivesPerPut']};",
        "pub const MAX_RAW_ARCHIVE_AGE_BACKFILL_ROWS_PER_PUT: usize = "
        f"{raw_archives['maximumAgeBackfillRowsPerPut']};",
        "pub const RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES: u64 = "
        f"{raw_archives['freeSpaceWireMaximumBytes']};",
        f'pub const RESEARCH_ARTIFACT_DOCUMENT_NAMESPACE: &str = "{research_artifacts["physicalNamespace"]}";',
        f'pub const RESEARCH_ARTIFACT_STATE_NAMESPACE: &str = "{research_artifacts["stateNamespace"]}";',
        f'pub const RESEARCH_ARTIFACT_CREATED_INDEX_NAMESPACE: &str = "{research_artifacts["createdIndexNamespace"]}";',
        f'pub const RESEARCH_ARTIFACT_COUNT_NAMESPACE: &str = "{research_artifacts["countNamespace"]}";',
        f'pub const MAX_RESEARCH_ARTIFACTS_PER_OWNER: usize = {research_artifacts["maximumRowsPerOwner"]};',
        f'pub const MAX_RESEARCH_DIRECTION_ROWS: usize = {research_artifacts["maximumDirectionRows"]};',
        f'pub const MAX_RESEARCH_DIRECTION_SCAN_ROWS: usize = {research_artifacts["maximumDirectionScanRows"]};',
        f'pub const MAX_RESEARCH_ARTIFACT_DOCUMENT_BYTES: usize = {research_artifacts["maximumDocumentBytes"]};',
        f'pub const MAX_RESEARCH_RESPONSE_BYTES: usize = {research_artifacts["maximumResponseBytes"]};',
        f'pub const RESEARCH_WORKSPACE_DOCUMENT_NAMESPACE: &str = "{research_workspaces["physicalNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_DOCUMENT_NAMESPACE: &str = "{optimizer_proposals["physicalNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_CREATED_INDEX_NAMESPACE: &str = "{optimizer_proposals["createdIndexNamespace"]}";',
        "pub const OPTIMIZER_PROPOSAL_STATUS_INDEX_NAMESPACE: &str =\n"
        f'    "{optimizer_proposals["statusCreatedIndexNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_STATUS_NAMESPACE: &str = "{optimizer_proposals["statusNamespace"]}";',
        f'pub const OPTIMIZER_PROPOSAL_COUNT_NAMESPACE: &str = "{optimizer_proposals["countNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_DOCUMENT_NAMESPACE: &str = "{optimizer_actions["physicalNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_APPLIED_INDEX_NAMESPACE: &str = "{optimizer_actions["appliedIndexNamespace"]}";',
        "pub const OPTIMIZER_ACTION_ACTIVE_APPLIED_INDEX_NAMESPACE: &str =\n"
        f'    "{optimizer_actions["activeAppliedIndexNamespace"]}";',
        "pub const OPTIMIZER_ACTION_PROPOSAL_INDEX_NAMESPACE: &str =\n"
        f'    "{optimizer_actions["proposalIndexNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_EXPIRY_INDEX_NAMESPACE: &str = "{optimizer_actions["expiryIndexNamespace"]}";',
        f'pub const OPTIMIZER_ACTION_COUNT_NAMESPACE: &str = "{optimizer_actions["countNamespace"]}";',
        f'pub const DEFAULT_OPTIMIZER_LIST_ROWS: usize = {optimizer_proposals["defaultListRows"]};',
        "pub const MAX_PROVIDER_TENANT_LABEL_CHARACTERS: usize = "
        f"{providers['maximumTenantLabelCharacters']};",
        f'pub const MODEL_ROUTING_AUTHORITY_NAMESPACE: &str = "{model_routing["authorityNamespace"]}";',
        "pub const MODEL_ROUTING_AUTHORITY_METADATA_NAMESPACE: &str = "
        f'"{model_routing["authorityMetadataNamespace"]}";',
        f'pub const MODEL_ROUTING_BACKUP_NAMESPACE: &str = "{model_routing["backupNamespace"]}";',
        "pub const MODEL_ROUTING_MIGRATION_RECEIPT_NAMESPACE: &str = "
        f'"{model_routing["migrationReceiptNamespace"]}";',
        f'pub const MODEL_ROUTING_SECRET_NAMESPACE: &str = "{model_routing["secretNamespace"]}";',
        f'pub const MODEL_ROUTING_SECRET_COUNT_NAMESPACE: &str = "{model_routing["secretCountNamespace"]}";',
        "pub const MODEL_ROUTING_SECRET_REFERENCE_INDEX_NAMESPACE: &str =\n"
        f'    "{model_routing["secretReferenceIndexNamespace"]}";',
        "pub const MODEL_ROUTING_SECRET_UPDATED_INDEX_NAMESPACE: &str = "
        f'"{model_routing["secretUpdatedIndexNamespace"]}";',
        "pub const MAX_MODEL_ROUTING_MIGRATION_CIPHERTEXT_BYTES: usize = "
        f"{model_routing['maximumMigrationCiphertextBytes']};",
        "pub const MAX_MODEL_ROUTING_SECRET_REFERENCE_CHARACTERS: usize = "
        f"{model_routing['maximumSecretReferenceCharacters']};",
        "pub const MAX_MODEL_ROUTING_CIPHERTEXT_CHARACTERS: usize = "
        f"{model_routing['maximumCiphertextCharacters']};",
        "pub const MAX_MODEL_ROUTING_KEY_HINT_CHARACTERS: usize = "
        f"{model_routing['maximumKeyHintCharacters']};",
        "pub const MAX_MODEL_ROUTING_PRUNED_PER_COMMAND: usize = "
        f"{model_routing['maximumPrunedPerCommand']};",
        "pub const MAX_MODEL_ROUTING_TIMESTAMP_SECONDS: f64 = "
        f"{model_routing['maximumTimestampSeconds']}.0;",
    ]
