"""Validate conversations table contracts.
Entries: named validate_* functions, called in the original order by validation.load_ir. No I/O."""

def validate_worker_jobs(worker_jobs) -> None:
    if (
        not isinstance(worker_jobs, dict)
        or worker_jobs.get("ownerKey") != "user_id"
        or worker_jobs.get("physicalFamily") != "entity+blob"
        or worker_jobs.get("physicalNamespace") != "worker_job_documents"
        or worker_jobs.get("idempotencyNamespace") != "worker_job_idempotency"
        or worker_jobs.get("queuedIndexNamespace") != "worker_jobs_queued"
        or worker_jobs.get("queuedSummaryNamespace") != "worker_jobs_queued_summary"
        or worker_jobs.get("leaseIndexNamespace") != "worker_jobs_by_lease"
        or worker_jobs.get("maximumPayloadBytes") != 1024 * 1024
        or worker_jobs.get("maximumErrorBytes") != 64 * 1024
        or worker_jobs.get("maximumClaimTaskKinds") != 32
        or worker_jobs.get("maximumPriority") != 1000
        or worker_jobs.get("maximumClockMilliseconds") != 9_223_372_036_854_775_000
        or worker_jobs.get("maximumFutureClockSkewMilliseconds") != 86_400_000
    ):
        raise ValueError("Tofu-DB worker jobs must be bounded, indexed, and fenced")


def validate_events(events) -> None:
    if not isinstance(events, dict) or events.get("physicalFamily") != "entity+stream":
        raise ValueError("Tofu-DB events table must bind its index and stream")


def validate_archives(archives) -> None:
    if (
        not isinstance(archives, dict)
        or archives.get("ownerKey") != "user_id"
        or archives.get("physicalFamily") != "entity+blob"
        or archives.get("physicalNamespace") != "compaction_archive_metadata"
        or archives.get("messagesNamespace") != "compaction_archive_messages"
        or archives.get("postMessagesNamespace")
        != "compaction_archive_post_messages"
        or archives.get("summaryNamespace") != "compaction_archive_summaries"
        or archives.get("receiptNamespace") != "compaction_archive_receipts"
        or archives.get("conversationIndexNamespace")
        != "compaction_archives_by_conversation"
        or archives.get("conversationCountNamespace") != "compaction_archive_counts"
        or archives.get("tenantGlobalClaimNamespace")
        != "compaction_archive_id_claims"
        or archives.get("maximumArchivesPerConversation") != 1000
        or archives.get("maximumDocumentBytes") != 67108864
    ):
        raise ValueError("Tofu-DB archives table must be bounded and owner scoped")


def validate_conversations(conversations) -> None:
    if (
        not isinstance(conversations, dict)
        or conversations.get("ownerKey") != "user_id"
        or conversations.get("tenantGlobalClaimNamespace") != "conversation_id_claims"
        or conversations.get("updatedIndexNamespace") != "conversation_by_updated"
        or conversations.get("activityCandidateIndexNamespace")
        != "conversation_activity_candidates_v2"
        or conversations.get("activityCandidateStateNamespace")
        != "conversation_activity_candidate_state_v2"
        or conversations.get("maximumActivityCandidateBackfillRowsPerTransaction") != 256
        or conversations.get("maximumActivityCandidateBackfillSourceBytesPerTransaction")
        != 16 * 1024 * 1024
        or conversations.get("executionEpochNamespace") != "conversation_execution_epochs"
        or conversations.get("trashMetadataNamespace") != "conversation_trash_metadata"
        or conversations.get("trashAgeIndexNamespace") != "conversation_trash_by_age"
        or conversations.get("trashSettingsOverlayNamespace")
        != "conversation_trash_settings_overlays"
        or conversations.get("recencyDirectoryNamespace")
        != "conversation_recency_directory"
        or conversations.get("recencyDirectoryStateNamespace")
        != "conversation_recency_directory_state"
        or conversations.get("maximumRecencyDirectoryEntries") != 128
        or conversations.get("maximumRecencyDirectoryBackfillRowsPerTransaction") != 64
        or conversations.get("projectRelinkAdmission") != "complete-owner-scoped-active-and-trash-without-count-quota"
        or "maximumProjectRelinkConversationCandidates" in conversations
    ):
        raise ValueError("Tofu-DB conversations table must preserve scoped ownership and ID claims")


def validate_turns(turns) -> None:
    if (
        not isinstance(turns, dict)
        or turns.get("ownerKey") != "user_id"
        or turns.get("physicalFamily") != "entity+blob"
        or turns.get("laneIndexNamespace") != "turns_by_lane_ordinal"
        or turns.get("activityIndexNamespace") != "turn_activity_timestamps"
        or turns.get("maximumActivityTurnRowsPerQuery") != 100_000
        or turns.get("laneCountNamespace") != "turn_lane_counts"
        or turns.get("turnIdClaimNamespace") != "turn_id_claims"
        or turns.get("attemptIdClaimNamespace") != "attempt_id_claims"
        or turns.get("attemptClaimLocatorVersion") != 1
        or turns.get("attemptCommandNamespace") != "generation_attempt_commands"
        or turns.get("attemptDispatchableIndexNamespace")
        != "generation_attempts_dispatchable"
        or turns.get("maximumDispatchableAttemptsPerQuery") != 32
        or turns.get("laneCompactionIndexNamespace")
        != "turns_by_lane_compaction"
        or turns.get("maximumCompactionMetadataRows") != 100_000
        or turns.get("maximumCompactionProjectionUpdates") != 512
        or turns.get("maximumCompactionReparentedTurns") != 512
        or turns.get("maximumCompactionProjectionBytes") != 8 * 1024 * 1024
        or turns.get("maximumCompactionMaterializedBytes") != 64 * 1024 * 1024
        or turns.get("attemptRecoveryIndexNamespace")
        != "generation_attempts_live_by_created"
        or turns.get("laneLiveAttemptNamespace") != "turn_lane_live_attempts"
        or turns.get("maximumRecoveryRowsPerTransaction") != 500
        or turns.get("maximumRecoveryIndexRowsPerOwner") != 10_000
        or turns.get("maximumRecoveryProjectionBytesPerTransaction") != 8 * 1024 * 1024
        or turns.get("attemptTimingTaskIndexNamespace")
        != "generation_attempts_by_timing_task"
        or turns.get("attemptTimingConversationIndexNamespace")
        != "generation_attempts_by_conversation_created_desc"
        or turns.get("maximumTimingTraceRowsPerQuery") != 100
        or turns.get("maximumTimingTraceTaskCandidates") != 100
        or turns.get("maximumTimingTraceClientObservations") != 64
        or turns.get("maximumTimingTracePersistedBytes") != 96 * 1024
        or turns.get("maximumTimingTraceCounter") != 2_147_483_647
        or turns.get("projectionHeadNamespace") != "turn_projection_heads"
        or turns.get("metadataSummaryNamespace") != "turn_metadata_summaries"
        or turns.get("maximumTurnProjectionInlineLiveBytes") != 64 * 1024
        or turns.get("maximumTurnProjectionHeadPatches") != 64
        or turns.get("maximumTurnProjectionTextPageBytes") != 16 * 1024
        or turns.get("maximumTurnProjectionTextAppendBytes") != 64 * 1024
        or turns.get("maximumTurnProjectionPatchBytes") != 1024 * 1024
        or "maximumTurnProjectionWirePatchBytes" in turns
        or turns.get("maximumTurnProjectionPatchDepth") != 128
        or turns.get("maximumTurnProjectionPatchOperations") != 65_536
        or turns.get("maximumLegacyTurnImages") != 20
        or turns.get("maximumLegacyTurnImageBytes") != 8 * 1024 * 1024
        or turns.get("attemptTurnCountNamespace") != "generation_attempt_turn_counts"
        or turns.get("attemptTurnDirectoryNamespace") != "generation_attempts_by_turn"
        or turns.get("maximumAttemptsPerTurn") != 64
        or turns.get("maximumAttemptTurnDirectoryBytes") != 65_536
        or turns.get("maximumAttemptTombstoneBytes") != 16_384
        or turns.get("attemptEventHeadNamespace") != "attempt_event_heads"
        or turns.get("attemptEventNamespace") != "attempt_events"
        or turns.get("maximumAttemptEventDocumentBytes") != 32 * 1024 * 1024
        or turns.get("maximumAttemptEventPageBytes") != 64 * 1024 * 1024 - 4096
        or turns.get("attemptEventRetentionIndexNamespace")
        != "attempt_events_by_settled"
        or turns.get("syncAgeIndexNamespace")
        != "conversation_sync_events_by_created"
        or turns.get("attemptEventSyncReferenceNamespace")
        != "attempt_event_sync_references"
        or turns.get("maximumSyncPruneRowsPerTransaction") != 1_000
        or turns.get("maximumSyncPruneMaterializedBytes") != 64 * 1024 * 1024
        or turns.get("maximumAttemptEventPruneAttemptsPerTransaction") != 64
        or turns.get("maximumAttemptEventPruneRowsPerTransaction") != 200_000
        or turns.get("maximumAttemptEventPruneMaterializedBytes") != 64 * 1024 * 1024
        or turns.get("updatedIndexNamespace") != "turns_by_updated"
        or turns.get("tombstoneNamespace") != "turn_tombstones"
        or turns.get("tombstoneAgeIndexNamespace") != "turn_tombstones_by_age"
        or turns.get("supersededAttemptIndexNamespace")
        != "generation_attempts_by_superseded"
        or turns.get("maximumCleanupAttemptsPerTransaction") != 64
        or turns.get("maximumCleanupIndexScanRowsPerTransaction") != 256
    ):
        raise ValueError(
            "Tofu-DB turns table must preserve owner scope, global identity claims, "
            "and lane ordering"
        )
