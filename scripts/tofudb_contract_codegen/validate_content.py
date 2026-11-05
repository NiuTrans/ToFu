"""Validate content table contracts.
Entries: named validate_* functions, called in the original order by validation.load_ir. No I/O."""

def validate_paper_library(paper_library) -> None:
    if (
        not isinstance(paper_library, dict)
        or paper_library.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_library.get("physicalFamily") != "entity+blob"
        or paper_library.get("coreNamespace") != "paper_library_core_documents"
        or paper_library.get("stateNamespace") != "paper_library_state_documents"
        or paper_library.get("updatedIndexNamespace") != "paper_library_by_updated_desc"
        or paper_library.get("hashIndexNamespace") != "paper_library_by_hash_updated_desc"
        or paper_library.get("arxivIndexNamespace") != "paper_library_by_arxiv_updated_desc"
        or paper_library.get("reportPresenceNamespace") != "paper_report_presence"
        or paper_library.get("countNamespace") != "paper_library_counts"
        or paper_library.get("maximumRowsPerOwner") != 1024
        or paper_library.get("maximumCoreDocumentBytes") != 64 * 1024 * 1024
        or paper_library.get("maximumStateDocumentBytes") != 64 * 1024
        or paper_library.get("maximumResponseBytes") != 64 * 1024 * 1024
        or paper_library.get("maximumFaninPapers") != 40
        or paper_library.get("maximumFaninTextCharacters") != 6000
        or paper_library.get("maximumQaTextCharacters") != 1_000_000
    ):
        raise ValueError("Tofu-DB paper library must be bounded and owner scoped")


def validate_paper_notes(paper_notes) -> None:
    if (
        not isinstance(paper_notes, dict)
        or paper_notes.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_notes.get("physicalFamily") != "entity+blob"
        or paper_notes.get("physicalNamespace") != "paper_note_documents"
        or paper_notes.get("listIndexNamespace") != "paper_notes_by_paper_lang_created"
        or paper_notes.get("countNamespace") != "paper_note_counts"
        or paper_notes.get("maximumRowsPerOwner") != 4096
        or paper_notes.get("maximumDocumentBytes") != 512 * 1024
        or paper_notes.get("maximumResponseBytes") != 64 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB paper notes must be bounded and owner scoped")


def validate_plugin_manifests(plugin_manifests) -> None:
    if (
        not isinstance(plugin_manifests, dict)
        or plugin_manifests.get("ownerKey") != "authenticated_tenant_and_owner"
        or plugin_manifests.get("physicalFamily") != "entity+blob"
        or plugin_manifests.get("physicalNamespace") != "plugin_manifest_documents"
        or plugin_manifests.get("maximumNamespaceCharacters") != 128
        or plugin_manifests.get("maximumManifestBytes") != 4 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB plugin manifests must be bounded and owner scoped")


def validate_knowledge_documents(knowledge_documents) -> None:
    if (
        not isinstance(knowledge_documents, dict)
        or knowledge_documents.get("ownerKey") != "authenticated_tenant_and_owner"
        or knowledge_documents.get("physicalFamily") != "entity+blob"
        or knowledge_documents.get("documentNamespace") != "knowledge_document_documents"
        or knowledge_documents.get("chunkNamespace") != "knowledge_chunk_documents"
        or knowledge_documents.get("assetNamespace") != "knowledge_asset_documents"
        or knowledge_documents.get("assetOrdinalIndexNamespace") != "knowledge_asset_ordinals"
        or knowledge_documents.get("assetChunkReverseNamespace") != "knowledge_asset_chunk_reverse"
        or knowledge_documents.get("termNamespace") != "knowledge_terms"
        or knowledge_documents.get("digestIndexNamespace") != "knowledge_documents_by_sha256"
        or knowledge_documents.get("settingsNamespace") != "knowledge_settings"
        or knowledge_documents.get("chunkAssetLinkNamespace") != "knowledge_chunk_asset_links"
        or knowledge_documents.get("enrichmentQueueNamespace") != "knowledge_enrichment_queue"
        or knowledge_documents.get("enrichmentOwnerIndexNamespace") != "knowledge_enrichment_owners"
        or knowledge_documents.get("maximumDocumentIdCharacters") != 128
        or knowledge_documents.get("maximumAssetIdCharacters") != 128
        or knowledge_documents.get("maximumNameCharacters") != 240
        or knowledge_documents.get("maximumStoredNameCharacters") != 512
        or knowledge_documents.get("maximumDocumentKindCharacters") != 32
        or knowledge_documents.get("maximumAssetKindCharacters") != 64
        or knowledge_documents.get("maximumMimeTypeCharacters") != 255
        or knowledge_documents.get("maximumMethodCharacters") != 255
        or knowledge_documents.get("maximumRelationCharacters") != 64
        or knowledge_documents.get("maximumStatusCharacters") != 32
        or knowledge_documents.get("maximumScopeCharacters") != 32
        or knowledge_documents.get("maximumSectionCharacters") != 20_000
        or knowledge_documents.get("maximumLocationCharacters") != 20_000
        or knowledge_documents.get("maximumChunkContentCharacters") != 4_000_000
        or knowledge_documents.get("maximumSearchTextCharacters") != 4_000_000
        or knowledge_documents.get("maximumAssetTextCharacters") != 1_000_000
        or knowledge_documents.get("maximumCaptionCharacters") != 20_000
        or knowledge_documents.get("maximumEnrichmentModelCharacters") != 512
        or knowledge_documents.get("maximumEnrichmentErrorCharacters") != 2_000
        or knowledge_documents.get("maximumMediaMetadataCharacters") != 100_000
        or knowledge_documents.get("maximumAssetMetadataCharacters") != 20_000
        or knowledge_documents.get("maximumJsonArrayCharacters") != 200_000
        or knowledge_documents.get("maximumTermCharacters") != 128
        or knowledge_documents.get("maximumSearchTokens") != 32
        or knowledge_documents.get("maximumPageRows") != 200
        or knowledge_documents.get("maximumCatalogPageSize") != 100
        or knowledge_documents.get("maximumCatalogQueryCharacters") != 200
        or knowledge_documents.get("maximumEnrichmentOwnerRows") != 512
        or knowledge_documents.get("maximumDocumentBytes") != 1024 * 1024
        or knowledge_documents.get("maximumChunkDocumentBytes") != 32 * 1024 * 1024
        or knowledge_documents.get("maximumAssetDocumentBytes") != 16 * 1024 * 1024
        or knowledge_documents.get("maximumSearchPostingsScan") != 262_144
        or knowledge_documents.get("maximumCatalogScanDocuments") != 65_536
        or knowledge_documents.get("maximumClaimScanRows") != 4_096
        or knowledge_documents.get("maximumChunksPerDocument") != 65_536
        or knowledge_documents.get("maximumAssetsPerDocument") != 65_536
        or knowledge_documents.get("maximumOwnerAssetRows") != 262_144
    ):
        raise ValueError("Tofu-DB knowledge documents must be bounded and owner scoped")


def validate_paper_reports(paper_reports) -> None:
    if (
        not isinstance(paper_reports, dict)
        or paper_reports.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_reports.get("coreNamespace") != "paper_report_core_documents"
        or paper_reports.get("metaNamespace") != "paper_report_meta_documents"
        or paper_reports.get("latestIndexNamespace") != "paper_reports_by_hash_created_desc"
        or paper_reports.get("presenceNamespace") != "paper_report_presence"
        or paper_reports.get("countNamespace") != "paper_report_counts"
        or paper_reports.get("maximumRowsPerOwner") != 4096
        or paper_reports.get("maximumDocumentBytes") != 64 * 1024 * 1024
        or paper_reports.get("maximumMetaBytes") != 8 * 1024 * 1024
        or paper_reports.get("maximumResponseBytes") != 64 * 1024 * 1024
        or paper_reports.get("maximumReopenSiblings") != 8
        or paper_reports.get("maximumExcerptHashes") != 40
        or paper_reports.get("maximumExcerptTextCharacters") != 6000
    ):
        raise ValueError("Tofu-DB paper reports must be bounded and owner scoped")


def validate_paper_translations(paper_translations) -> None:
    if (
        not isinstance(paper_translations, dict)
        or paper_translations.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_translations.get("documentNamespace") != "paper_translation_documents"
        or paper_translations.get("countNamespace") != "paper_translation_counts"
        or paper_translations.get("maximumRowsPerOwner") != 4096
        or paper_translations.get("maximumDocumentBytes") != 16 * 1024 * 1024
        or paper_translations.get("maximumResponseBytes") != 16 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB paper translations must be bounded and owner scoped")


def validate_paper_podcasts(paper_podcasts) -> None:
    if (
        not isinstance(paper_podcasts, dict)
        or paper_podcasts.get("ownerKey") != "authenticated_tenant_and_owner"
        or paper_podcasts.get("physicalFamily") != "entity+blob"
        or paper_podcasts.get("coreNamespace") != "paper_podcast_core_documents"
        or paper_podcasts.get("stateNamespace") != "paper_podcast_state_documents"
        or paper_podcasts.get("countNamespace") != "paper_podcast_counts"
        or paper_podcasts.get("activeIndexNamespace") != "paper_podcast_generating_by_tenant"
        or paper_podcasts.get("activeCountNamespace") != "paper_podcast_generating_counts"
        or paper_podcasts.get("maximumRowsPerOwner") != 4096
        or paper_podcasts.get("maximumActivePerTenant") != 256
        or paper_podcasts.get("maximumScriptBytes") != 8 * 1024 * 1024
        or paper_podcasts.get("maximumMetaBytes") != 4 * 1024 * 1024
        or paper_podcasts.get("maximumCoreDocumentBytes") != 16 * 1024 * 1024
        or paper_podcasts.get("maximumStateDocumentBytes") != 64 * 1024
        or paper_podcasts.get("maximumResponseBytes") != 16 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB paper podcasts must be bounded and owner scoped")


def validate_raw_archives(raw_archives) -> None:
    if (
        not isinstance(raw_archives, dict)
        or raw_archives.get("ownerKey") != "authenticated_tenant_and_owner"
        or raw_archives.get("physicalFamily") != "entity+blob"
        or raw_archives.get("physicalNamespace") != "raw_archive_documents"
        or raw_archives.get("idClaimNamespace") != "raw_archive_id_claims"
        or raw_archives.get("tenantUsageNamespace") != "raw_archive_tenant_usage"
        or raw_archives.get("attemptUsageNamespace") != "raw_archive_attempt_usage"
        or raw_archives.get("ownerCountNamespace") != "raw_archive_owner_counts"
        or raw_archives.get("taskIndexNamespace") != "raw_archives_by_task_created"
        or raw_archives.get("taskRoundIndexNamespace")
        != "raw_archives_by_task_round_created"
        or raw_archives.get("attemptIndexNamespace") != "raw_archives_by_attempt_created"
        or raw_archives.get("conversationIndexNamespace")
        != "raw_archives_by_conversation_created"
        or raw_archives.get("conversationStatsNamespace")
        != "raw_archive_conversation_stats"
        or raw_archives.get("ownerAgeIndexNamespace") != "raw_archives_by_owner_created"
        or raw_archives.get("ownerAgeBackfillNamespace")
        != "raw_archive_owner_age_backfill"
        or raw_archives.get("maximumStoredBytes") != 16 * 1024 * 1024
        or raw_archives.get("maximumDocumentBytes") != 24 * 1024 * 1024
        or raw_archives.get("maximumDecodedPartBytes") != 64 * 1024 * 1024
        or raw_archives.get("maximumTenantBudgetBytes") != 4 * 1024 * 1024 * 1024
        or raw_archives.get("maximumReadChunkBytes") != 1024 * 1024
        or raw_archives.get("maximumArchivesPerOwner") != 65_536
        or raw_archives.get("maximumArchivesPerConversation") != 1792
        or raw_archives.get("maximumSummaryBytes") != 64 * 1024
        or raw_archives.get("maximumSummaryKeys") != 32
        or raw_archives.get("maximumListRows") != 64
        or raw_archives.get("maximumReclaimedArchivesPerPut") != 64
        or raw_archives.get("maximumAgeBackfillRowsPerPut") != 512
        or raw_archives.get("freeSpaceWireMaximumBytes") != 1_000_000_000_000_000
    ):
        raise ValueError("Tofu-DB raw archives must be bounded and owner scoped")


def validate_research_artifacts(research_artifacts) -> None:
    if (
        not isinstance(research_artifacts, dict)
        or research_artifacts.get("ownerKey") != "authenticated_tenant_and_owner"
        or research_artifacts.get("physicalFamily") != "entity+blob"
        or research_artifacts.get("physicalNamespace") != "research_artifact_documents"
        or research_artifacts.get("stateNamespace") != "research_artifact_state"
        or research_artifacts.get("createdIndexNamespace") != "research_artifacts_by_created_desc"
        or research_artifacts.get("countNamespace") != "research_artifact_counts"
        or research_artifacts.get("maximumRowsPerOwner") != 4096
        or research_artifacts.get("maximumDirectionRows") != 1000
        or research_artifacts.get("maximumDirectionScanRows") != 2000
        or research_artifacts.get("maximumDocumentBytes") != 64 * 1024 * 1024
        or research_artifacts.get("maximumResponseBytes") != 64 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB research artifacts must be bounded and owner scoped")


def validate_research_workspaces(research_workspaces) -> None:
    if (
        not isinstance(research_workspaces, dict)
        or research_workspaces.get("ownerKey") != "authenticated_tenant_and_owner"
        or research_workspaces.get("physicalFamily") != "entity+blob"
        or research_workspaces.get("physicalNamespace") != "research_workspace_documents"
        or "maximumDocumentBytes" in research_workspaces
    ):
        raise ValueError("Tofu-DB research workspaces must retain complete owner-scoped documents")
