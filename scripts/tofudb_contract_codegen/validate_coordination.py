"""Validate coordination table contracts.
Entries: named validate_* functions, called in the original order by validation.load_ir. No I/O."""

def validate_orchestration_definitions(orchestration_definitions) -> None:
    if (
        not isinstance(orchestration_definitions, dict)
        or orchestration_definitions.get("ownerKey") != "authenticated_tenant_and_owner"
        or orchestration_definitions.get("physicalFamily") != "entity+blob"
        or orchestration_definitions.get("documentNamespace")
        != "orchestration_definition_documents"
        or orchestration_definitions.get("tenantGlobalIdClaimNamespace")
        != "orchestration_definition_id_claims"
        or orchestration_definitions.get("ownerUpdatedIndexNamespace")
        != "orchestration_definitions_by_updated_desc"
        or orchestration_definitions.get("ownerCountNamespace")
        != "orchestration_definition_counts"
        or orchestration_definitions.get("maximumDefinitionsPerOwner") != 1000
        or orchestration_definitions.get("maximumDefinitionDocumentBytes")
        != 8 * 1024 * 1024
        or orchestration_definitions.get("maximumDefinitionIdCharacters") != 200
        or orchestration_definitions.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB orchestration definitions must be bounded and exact")


def validate_orchestration_runs(orchestration_runs) -> None:
    if (
        not isinstance(orchestration_runs, dict)
        or orchestration_runs.get("ownerKey")
        != "authenticated_tenant_and_owner_or_internal_tenant_maintenance"
        or orchestration_runs.get("physicalFamily") != "entity+blob"
        or orchestration_runs.get("coreNamespace") != "orchestration_run_cores"
        or orchestration_runs.get("stateNamespace") != "orchestration_run_states"
        or orchestration_runs.get("tenantGlobalIdClaimNamespace")
        != "orchestration_run_id_claims"
        or orchestration_runs.get("ownerCreatedIndexNamespace")
        != "orchestration_runs_by_created_desc"
        or orchestration_runs.get("ownerStatusCreatedIndexNamespace")
        != "orchestration_runs_by_status_created_desc"
        or orchestration_runs.get("ownerOrchestrationCreatedIndexNamespace")
        != "orchestration_runs_by_orchestration_created_desc"
        or orchestration_runs.get("tenantGlobalActiveIndexNamespace")
        != "orchestration_runs_global_active"
        or orchestration_runs.get("tenantGlobalPendingContinuityIndexNamespace")
        != "orchestration_runs_pending_continuity"
        or orchestration_runs.get("goalActiveClaimNamespace")
        != "orchestration_goal_active_runs"
        or orchestration_runs.get("ownerGoalCreatedIndexNamespace")
        != "orchestration_goal_runs_by_created_desc"
        or orchestration_runs.get("eventDocumentNamespace")
        != "orchestration_run_event_documents"
        or orchestration_runs.get("ownerCountNamespace") != "orchestration_run_counts"
        or orchestration_runs.get("maximumRunsPerOwner") != 10_000
        or orchestration_runs.get("maximumRunCoreBytes") != 8 * 1024 * 1024
        or orchestration_runs.get("maximumRunStateBytes") != 8 * 1024 * 1024
        or orchestration_runs.get("maximumEventsPerRun") != 100_000
        or orchestration_runs.get("maximumEventDocumentBytes") != 1024 * 1024
        or orchestration_runs.get("maximumEventPageRows") != 2000
        or orchestration_runs.get("maximumListRows") != 200
        or orchestration_runs.get("maximumMaintenanceRows") != 1000
        or orchestration_runs.get("maximumRunIdCharacters") != 200
        or orchestration_runs.get("maximumTenantLabelCharacters") != 256
        or orchestration_runs.get("maximumResponseBytes") != 8 * 1024 * 1024
    ):
        raise ValueError("Tofu-DB orchestration runs must be bounded and exact")


def validate_swarm(swarm: object) -> None:
    if (
        not isinstance(swarm, dict)
        or swarm.get("ownerKey") != "authenticated_tenant_and_owner"
        or swarm.get("physicalFamily") != "entity+blob"
        or swarm.get("sessionDocumentNamespace") != "swarm_session_documents"
        or swarm.get("sessionStateNamespace") != "swarm_session_states"
        or swarm.get("tenantGlobalKeyClaimNamespace") != "swarm_session_key_claims"
        or swarm.get("ownerResumableIndexNamespace")
        != "swarm_sessions_resumable_by_updated_desc"
        or swarm.get("ownerSessionCountNamespace") != "swarm_session_counts"
        or swarm.get("agentCoreNamespace") != "swarm_agent_cores"
        or swarm.get("agentStateNamespace") != "swarm_agent_states"
        or swarm.get("agentCountNamespace") != "swarm_agent_counts"
        or swarm.get("maximumSessionsPerOwner") != 512
        or swarm.get("maximumAgentsPerSession") != 1000
        or swarm.get("maximumSessionDocumentBytes") != 8 * 1024 * 1024
        or swarm.get("maximumAgentCoreBytes") != 64 * 1024 * 1024 - 65536
        or swarm.get("maximumResponseBytes") != 64 * 1024 * 1024 - 4096
        or swarm.get("maximumSwarmKeyCharacters") != 512
        or swarm.get("maximumAgentIdCharacters") != 512
        or swarm.get("maximumObjectiveCharacters") != 100_000
        or swarm.get("maximumAgentIdsPerDelivery") != 1000
        or swarm.get("maximumRoundsUsed") != 1_000_000
        or swarm.get("checkpointMarkerResultFields") != [
            "actuals_complete", "checkpoint_phase", "completion_tokens", "cost_usd",
            "elapsed_seconds", "prompt_tokens", "rounds_used", "total_tokens"]
        or swarm.get("maximumCheckpointMarkerTextCharacters") != 64
    ):
        raise ValueError("Tofu-DB swarm persistence must be bounded and owner scoped")


def validate_integration(integration) -> None:
    if (
        not isinstance(integration, dict)
        or integration.get("ownerKey")
        != "authenticated_owner_or_internal_tenant_worker"
        or integration.get("physicalFamily") != "entity"
        or integration.get("workspaceNamespace")
        != "integration_workspace_documents"
        or integration.get("naturalKeyClaimNamespace")
        != "integration_workspace_natural_claims"
        or integration.get("rowLocatorNamespace")
        != "integration_workspace_row_locators"
        or integration.get("rowSequenceNamespace")
        != "integration_workspace_row_sequence"
        or integration.get("globalReadyIndexNamespace")
        != "integration_workspaces_ready_by_updated_asc"
        or integration.get("globalIntegratingIndexNamespace")
        != "integration_workspaces_integrating_by_updated_asc"
        or integration.get("globalProjectActiveClaimNamespace")
        != "integration_project_active_claims"
        or integration.get("globalActiveCountNamespace") != "integration_active_count"
        or integration.get("ownerProjectUpdatedIndexNamespace")
        != "integration_workspaces_by_project_updated_desc"
        or integration.get("ownerWorkspaceCountNamespace")
        != "integration_workspace_counts"
        or integration.get("eventNamespace") != "integration_event_documents"
        or integration.get("eventSequenceNamespace") != "integration_event_sequence"
        or integration.get("eventCountNamespace") != "integration_event_counts"
        or integration.get("workspaceAdmission") != "demand-without-count-quota"
        or "maximumActiveWorkspacesPerAuthority" in integration
        or "maximumWorkspacesPerOwner" in integration
        or integration.get("maximumStatusRows") != 1000
        or integration.get("maximumEventsPerProject") != 300
        or integration.get("maximumStatusEvents") != 30
        or integration.get("maximumProjectRootCharacters") != 4096
        or integration.get("maximumTaskIdCharacters") != 512
        or integration.get("maximumWorkspaceDocumentBytes") != 16_384
        or integration.get("maximumResponseBytes") != 8 * 1024 * 1024
        or integration.get("staleIntegratingSeconds") != 660
    ):
        raise ValueError("Tofu-DB integration workspaces must be bounded and exact")


def validate_queue(queue) -> None:
    if (
        not isinstance(queue, dict)
        or queue.get("ownerKey") != "authenticated_tenant_and_owner"
        or queue.get("physicalFamily") != "entity+blob"
        or queue.get("coreNamespace") != "queue_item_cores"
        or queue.get("stateNamespace") != "queue_item_states"
        or queue.get("tenantGlobalIdClaimNamespace") != "queue_item_id_claims"
        or queue.get("ownerConversationOrderIndexNamespace")
        != "queue_items_by_conversation_order"
        or queue.get("tenantGlobalConversationIndexNamespace")
        != "queue_conversations_by_kind_oldest"
        or queue.get("tenantGlobalLeaseIndexNamespace") != "queue_leases_by_expiry"
        or queue.get("autopilotMarkerNamespace") != "queue_autopilot_markers"
        or queue.get("tenantGlobalAutopilotIndexNamespace")
        != "queue_autopilot_by_owner_conversation"
        or queue.get("appendMetadata") != {
            "namespaceField": "stateNamespace",
            "keyOwner": "authenticated_owner_user_id",
            "key": "conversation_id",
            "itemStateKeyOwner": "tenant_global_owner_zero",
            "format": "tofu.queue-append/v1",
            "lifecycle": "same-transaction-maintenance;missing-legacy-entry-full-scan;delete-with-conversation",
            "realAppend": "point-read-count-and-kind-oldest-times;full-scan-when-goal-continuation-present",
        }
        or queue.get("itemAdmission") != "demand-without-count-quota"
        or "maximumItemsPerConversation" in queue
        or queue.get("coreAdmission") != "existing-owner-bound-blob-without-byte-quota"
        or queue.get("requestMeasurement") != "streamed-json-byte-count-without-debug-copy"
        or "maximumItemCoreBytes" in queue
        or queue.get("catalogAdmission") != "demand-without-count-quota"
        or "maximumGlobalConversationRows" in queue
        or "maximumAutopilotMarkers" in queue
        or queue.get("maximumReapItems") != 128
        or queue.get("defaultPageBytes") != 8 * 1024 * 1024
        or queue.get("responseAdmission") != "encoded-complete-result-without-private-byte-quota"
        or "maximumResponseBytes" in queue
        or queue.get("maximumIdCharacters") != 256
        or queue.get("maximumConversationIdCharacters") != 256
    ):
        raise ValueError("Tofu-DB conversation queue must be bounded and owner scoped")


def validate_scheduler(scheduler) -> None:
    if (
        not isinstance(scheduler, dict)
        or scheduler.get("ownerKey") != "authenticated_tenant_and_owner"
        or scheduler.get("physicalFamily") != "entity+blob"
        or scheduler.get("physicalNamespace") != "scheduled_task_documents"
        or scheduler.get("tenantGlobalIdClaimNamespace") != "scheduled_task_id_claims"
        or scheduler.get("systemKeyClaimNamespace") != "scheduled_task_system_key_claims"
        or scheduler.get("executionIndexNamespace") != "scheduled_tasks_by_execution"
        or scheduler.get("createdIndexNamespace") != "scheduled_tasks_by_created_desc"
        or scheduler.get("enabledCreatedIndexNamespace")
        != "scheduled_tasks_enabled_by_created_desc"
        or scheduler.get("tenantGlobalCreatedIndexNamespace")
        != "scheduled_tasks_global_by_created_desc"
        or scheduler.get("tenantGlobalEnabledCreatedIndexNamespace")
        != "scheduled_tasks_global_enabled_by_created_desc"
        or scheduler.get("ownerCountNamespace") != "scheduled_task_counts"
        or scheduler.get("pollDocumentNamespace") != "scheduler_poll_documents"
        or scheduler.get("pollTimeIndexNamespace")
        != "scheduler_polls_by_task_time_desc"
        or scheduler.get("pollSequenceNamespace") != "scheduler_poll_global_sequence"
        or "maximumTasksPerOwner" in scheduler
        or scheduler.get("maximumTaskDocumentBytes") != 1_048_576
        or scheduler.get("maximumTaskIdCharacters") != 128
        or scheduler.get("maximumSystemKeyCharacters") != 128
        or scheduler.get("listLimitZeroReadsAll") is not True
        or "maximumTaskListRows" in scheduler
        or scheduler.get("maximumPollRows") != 200
        or scheduler.get("maximumPollDocumentBytes") != 16_384
    ):
        raise ValueError("Tofu-DB scheduled-task physical contract is incomplete")


def validate_timers(timers) -> None:
    if (
        not isinstance(timers, dict)
        or timers.get("ownerKey") != "authenticated_tenant_and_owner"
        or timers.get("physicalFamily") != "entity+blob"
        or timers.get("physicalNamespace") != "timer_documents"
        or timers.get("tenantGlobalIdClaimNamespace") != "timer_id_claims"
        or timers.get("ownerStatusCreatedIndexNamespace") != "timers_by_status_created_desc"
        or timers.get("ownerConversationIndexNamespace") != "timers_by_conversation"
        or timers.get("tenantGlobalActiveCreatedIndexNamespace")
        != "timers_global_active_by_created_asc"
        or timers.get("ownerTotalCountNamespace") != "timer_total_counts"
        or timers.get("ownerActiveCountNamespace") != "timer_active_counts"
        or timers.get("pollDocumentNamespace") != "timer_poll_documents"
        or timers.get("pollTimeIndexNamespace") != "timer_polls_by_timer_time_desc"
        or timers.get("pollIdClaimNamespace") != "timer_poll_id_claims"
        or timers.get("pollSequenceNamespace") != "timer_poll_global_sequence"
        or timers.get("countPolicy") != "observational-counts-without-admission-quota"
        or timers.get("maximumTimerDocumentBytes") != 1_048_576
        or timers.get("maximumTimerIdCharacters") != 128
        or timers.get("maximumTimerListRows") != 200
        or timers.get("maximumGlobalActiveRows") != 2_000
        or timers.get("maximumPollRows") != 200
        or timers.get("maximumPollDocumentBytes") != 16_384
    ):
        raise ValueError("Tofu-DB timer physical contract is incomplete")


def validate_project_brain(project_brain) -> None:
    if (
        not isinstance(project_brain, dict)
        or project_brain.get("ownerKey") != "authenticated_owner_user_id"
        or project_brain.get("physicalFamily") != "entity+stream+blob"
        or project_brain.get("physicalNamespace") != "project_brain_documents"
        or project_brain.get("checkpointNamespace") != "project_brain_checkpoints"
        or project_brain.get("eventIndexNamespace") != "project_brain_event_positions"
        or project_brain.get("updatedIndexNamespace") != "project_brain_by_updated_desc"
        or project_brain.get("recoverySummaryNamespace") != "project_brain_recovery_v1"
        or project_brain.get("streamDomain") != "project_brain"
        or project_brain.get("maximumProjectKeyCharacters") != 4096
        or project_brain.get("collectionAdmission") != "complete-owner-scoped-project-history-without-count-quota"
        or project_brain.get("documentAdmission") != "blob-backed-without-private-byte-quota"
        or project_brain.get("maximumNarrativeTextBytes") != 720
        or project_brain.get("eventAdmission") != "complete-chunked-event-with-u32-format-length-and-paged-read"
        or project_brain.get("eventChunkBytes") != 256 * 1024 - 60
        or project_brain.get("eventCheckpointThreshold") != 1200
        or project_brain.get("eventCheckpointTail") != 600
    ):
        raise ValueError("Tofu-DB Project Brain must be bounded and owner scoped")


def validate_recent_projects(recent_projects) -> None:
    if (
        not isinstance(recent_projects, dict)
        or recent_projects.get("ownerKey") != "authenticated_owner_user_id"
        or recent_projects.get("physicalFamily") != "entity+blob"
        or recent_projects.get("physicalNamespace") != "recent_project_documents"
        or recent_projects.get("countNamespace") != "recent_project_counts"
        or recent_projects.get("admission") != "demand-without-owner-or-batch-count-quota"
        or "maximumProjectsPerOwner" in recent_projects
        or "maximumTouchBatch" in recent_projects
        or recent_projects.get("maximumPathCharacters") != 4096
    ):
        raise ValueError("Tofu-DB recent projects must be bounded and owner scoped")
