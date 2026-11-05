"""Project services constants into the native IR output.
Entry: render_services. Pure ordered projection of validated IR and shared input documents."""




def swarm_constants(swarm: dict) -> list[str]:
    return [
        f'pub const SWARM_SESSION_DOCUMENT_NAMESPACE: &str = "{swarm["sessionDocumentNamespace"]}";',
        f'pub const SWARM_SESSION_STATE_NAMESPACE: &str = "{swarm["sessionStateNamespace"]}";',
        f'pub const SWARM_SESSION_KEY_CLAIM_NAMESPACE: &str = "{swarm["tenantGlobalKeyClaimNamespace"]}";',
        f'pub const SWARM_RESUMABLE_INDEX_NAMESPACE: &str = "{swarm["ownerResumableIndexNamespace"]}";',
        f'pub const SWARM_SESSION_COUNT_NAMESPACE: &str = "{swarm["ownerSessionCountNamespace"]}";',
        f'pub const SWARM_AGENT_CORE_NAMESPACE: &str = "{swarm["agentCoreNamespace"]}";',
        f'pub const SWARM_AGENT_STATE_NAMESPACE: &str = "{swarm["agentStateNamespace"]}";',
        f'pub const SWARM_AGENT_COUNT_NAMESPACE: &str = "{swarm["agentCountNamespace"]}";',
        f"pub const MAX_SWARM_SESSIONS_PER_OWNER: usize = {swarm['maximumSessionsPerOwner']};",
        f"pub const MAX_SWARM_AGENTS_PER_SESSION: usize = {swarm['maximumAgentsPerSession']};",
        f"pub const MAX_SWARM_SESSION_DOCUMENT_BYTES: usize = {swarm['maximumSessionDocumentBytes']};",
        f"pub const MAX_SWARM_AGENT_CORE_BYTES: usize = {swarm['maximumAgentCoreBytes']};",
        f"pub const MAX_SWARM_RESPONSE_BYTES: usize = {swarm['maximumResponseBytes']};",
        f"pub const MAX_SWARM_KEY_CHARACTERS: usize = {swarm['maximumSwarmKeyCharacters']};",
        f"pub const MAX_SWARM_AGENT_ID_CHARACTERS: usize = {swarm['maximumAgentIdCharacters']};",
        f"pub const MAX_SWARM_OBJECTIVE_CHARACTERS: usize = {swarm['maximumObjectiveCharacters']};",
        f"pub const MAX_SWARM_AGENT_IDS_PER_DELIVERY: usize = {swarm['maximumAgentIdsPerDelivery']};",
        f"pub const MAX_SWARM_ROUNDS_USED: u64 = {swarm['maximumRoundsUsed']};",
        "pub const SWARM_CHECKPOINT_MARKER_RESULT_FIELDS: &[&str] = &["
        + ", ".join(f'"{name}"' for name in swarm["checkpointMarkerResultFields"]) + "];",
        f"pub const MAX_SWARM_CHECKPOINT_MARKER_TEXT_CHARACTERS: usize = {swarm['maximumCheckpointMarkerTextCharacters']};",
    ]


def render_services(tables) -> list[str]:
    browser_observations = tables["browser_site_observations"]
    records = tables["storage_records"]
    rate_limits = tables["rate_limit_events"]
    daily_costs = tables["daily_cost_cache"]
    orchestration_definitions = tables["orchestration_definitions"]
    orchestration_runs = tables["orchestration_runs"]
    swarm = tables["swarm_sessions"]
    integration = tables["integration_workspaces"]
    project_brain = tables["project_brain_projects"]
    scheduler = tables["scheduled_tasks"]
    queue = tables["conversation_queue"]
    timers = tables["timers"]
    recent_projects = tables["recent_projects"]
    providers = tables["byo_providers"]
    return [
        "pub const BROWSER_SITE_OBSERVATION_DOCUMENT_NAMESPACE: &str = "
        f'"{browser_observations["physicalNamespace"]}";',
        "pub const BROWSER_SITE_OBSERVATION_LRU_NAMESPACE: &str =\n"
        f'    "{browser_observations["lruNamespace"]}";',
        "pub const MAX_BROWSER_SITE_OBSERVATIONS_PER_OWNER: usize = "
        f"{browser_observations['maximumDocumentsPerOwner']};",
        "pub const MAX_BROWSER_SITE_OBSERVATION_BYTES: usize = "
        f"{browser_observations['maximumObservationPayloadBytes']};",
        "pub const MAX_BROWSER_SITE_OBSERVATION_STORED_BYTES: usize = "
        f"{browser_observations['maximumStoredDocumentBytes']};",
        f'pub const RECORD_DOCUMENT_NAMESPACE: &str = "{records["physicalNamespace"]}";',
        f'pub const RATE_LIMIT_BUCKET_NAMESPACE: &str = "{rate_limits["bucketNamespace"]}";',
        f'pub const RATE_LIMIT_EVENT_NAMESPACE: &str = "{rate_limits["eventNamespace"]}";',
        f'pub const RATE_LIMIT_EXPIRY_NAMESPACE: &str = "{rate_limits["expiryIndexNamespace"]}";',
        f'pub const RATE_LIMIT_COUNT_NAMESPACE: &str = "{rate_limits["countNamespace"]}";',
        f'pub const DAILY_COST_DOCUMENT_NAMESPACE: &str = "{daily_costs["physicalNamespace"]}";',
        f'pub const DAILY_COST_COUNT_NAMESPACE: &str = "{daily_costs["countNamespace"]}";',
        f'pub const ORCHESTRATION_DEFINITION_DOCUMENT_NAMESPACE: &str = "{orchestration_definitions["documentNamespace"]}";',
        f'pub const ORCHESTRATION_DEFINITION_ID_CLAIM_NAMESPACE: &str = "{orchestration_definitions["tenantGlobalIdClaimNamespace"]}";',
        "pub const ORCHESTRATION_DEFINITION_UPDATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_definitions["ownerUpdatedIndexNamespace"]}";',
        f'pub const ORCHESTRATION_DEFINITION_COUNT_NAMESPACE: &str = "{orchestration_definitions["ownerCountNamespace"]}";',
        f"pub const MAX_ORCHESTRATION_DEFINITIONS_PER_OWNER: usize = {orchestration_definitions['maximumDefinitionsPerOwner']};",
        f"pub const MAX_ORCHESTRATION_DEFINITION_DOCUMENT_BYTES: usize = {orchestration_definitions['maximumDefinitionDocumentBytes']};",
        f"pub const MAX_ORCHESTRATION_DEFINITION_ID_CHARACTERS: usize = {orchestration_definitions['maximumDefinitionIdCharacters']};",
        f"pub const MAX_ORCHESTRATION_RESPONSE_BYTES: usize = {orchestration_definitions['maximumResponseBytes']};",
        f'pub const ORCHESTRATION_RUN_CORE_NAMESPACE: &str = "{orchestration_runs["coreNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_STATE_NAMESPACE: &str = "{orchestration_runs["stateNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_ID_CLAIM_NAMESPACE: &str = "{orchestration_runs["tenantGlobalIdClaimNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_CREATED_INDEX_NAMESPACE: &str = "{orchestration_runs["ownerCreatedIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_STATUS_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["ownerStatusCreatedIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_ORCHESTRATION_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["ownerOrchestrationCreatedIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_GLOBAL_ACTIVE_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["tenantGlobalActiveIndexNamespace"]}";',
        "pub const ORCHESTRATION_RUN_PENDING_CONTINUITY_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["tenantGlobalPendingContinuityIndexNamespace"]}";',
        f'pub const ORCHESTRATION_GOAL_ACTIVE_CLAIM_NAMESPACE: &str = "{orchestration_runs["goalActiveClaimNamespace"]}";',
        "pub const ORCHESTRATION_GOAL_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{orchestration_runs["ownerGoalCreatedIndexNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_EVENT_DOCUMENT_NAMESPACE: &str = "{orchestration_runs["eventDocumentNamespace"]}";',
        f'pub const ORCHESTRATION_RUN_COUNT_NAMESPACE: &str = "{orchestration_runs["ownerCountNamespace"]}";',
        f"pub const MAX_ORCHESTRATION_RUNS_PER_OWNER: usize = {orchestration_runs['maximumRunsPerOwner']};",
        f"pub const MAX_ORCHESTRATION_RUN_CORE_BYTES: usize = {orchestration_runs['maximumRunCoreBytes']};",
        f"pub const MAX_ORCHESTRATION_RUN_STATE_BYTES: usize = {orchestration_runs['maximumRunStateBytes']};",
        f"pub const MAX_ORCHESTRATION_EVENTS_PER_RUN: usize = {orchestration_runs['maximumEventsPerRun']};",
        f"pub const MAX_ORCHESTRATION_EVENT_DOCUMENT_BYTES: usize = {orchestration_runs['maximumEventDocumentBytes']};",
        f"pub const MAX_ORCHESTRATION_EVENT_PAGE_ROWS: usize = {orchestration_runs['maximumEventPageRows']};",
        f"pub const MAX_ORCHESTRATION_RUN_LIST_ROWS: usize = {orchestration_runs['maximumListRows']};",
        f"pub const MAX_ORCHESTRATION_MAINTENANCE_ROWS: usize = {orchestration_runs['maximumMaintenanceRows']};",
        f"pub const MAX_ORCHESTRATION_RUN_ID_CHARACTERS: usize = {orchestration_runs['maximumRunIdCharacters']};",
        f"pub const MAX_ORCHESTRATION_TENANT_LABEL_CHARACTERS: usize = {orchestration_runs['maximumTenantLabelCharacters']};",
        *swarm_constants(swarm),
        f'pub const INTEGRATION_WORKSPACE_NAMESPACE: &str = "{integration["workspaceNamespace"]}";',
        f'pub const INTEGRATION_NATURAL_CLAIM_NAMESPACE: &str = "{integration["naturalKeyClaimNamespace"]}";',
        f'pub const INTEGRATION_ROW_LOCATOR_NAMESPACE: &str = "{integration["rowLocatorNamespace"]}";',
        f'pub const INTEGRATION_ROW_SEQUENCE_NAMESPACE: &str = "{integration["rowSequenceNamespace"]}";',
        f'pub const INTEGRATION_READY_INDEX_NAMESPACE: &str = "{integration["globalReadyIndexNamespace"]}";',
        "pub const INTEGRATION_INTEGRATING_INDEX_NAMESPACE: &str =",
        f'    "{integration["globalIntegratingIndexNamespace"]}";',
        f'pub const INTEGRATION_PROJECT_ACTIVE_CLAIM_NAMESPACE: &str = "{integration["globalProjectActiveClaimNamespace"]}";',
        f'pub const INTEGRATION_ACTIVE_COUNT_NAMESPACE: &str = "{integration["globalActiveCountNamespace"]}";',
        "pub const INTEGRATION_PROJECT_UPDATED_INDEX_NAMESPACE: &str =",
        f'    "{integration["ownerProjectUpdatedIndexNamespace"]}";',
        f'pub const INTEGRATION_WORKSPACE_COUNT_NAMESPACE: &str = "{integration["ownerWorkspaceCountNamespace"]}";',
        f'pub const INTEGRATION_EVENT_NAMESPACE: &str = "{integration["eventNamespace"]}";',
        f'pub const INTEGRATION_EVENT_SEQUENCE_NAMESPACE: &str = "{integration["eventSequenceNamespace"]}";',
        f'pub const INTEGRATION_EVENT_COUNT_NAMESPACE: &str = "{integration["eventCountNamespace"]}";',
        f"pub const MAX_INTEGRATION_STATUS_ROWS: usize = {integration['maximumStatusRows']};",
        f"pub const MAX_INTEGRATION_EVENTS_PER_PROJECT: usize = {integration['maximumEventsPerProject']};",
        f"pub const MAX_INTEGRATION_STATUS_EVENTS: usize = {integration['maximumStatusEvents']};",
        f"pub const MAX_INTEGRATION_PROJECT_ROOT_CHARACTERS: usize = {integration['maximumProjectRootCharacters']};",
        f"pub const MAX_INTEGRATION_TASK_ID_CHARACTERS: usize = {integration['maximumTaskIdCharacters']};",
        f"pub const MAX_INTEGRATION_WORKSPACE_DOCUMENT_BYTES: usize = {integration['maximumWorkspaceDocumentBytes']};",
        f"pub const MAX_INTEGRATION_RESPONSE_BYTES: usize = {integration['maximumResponseBytes']};",
        f"pub const INTEGRATION_STALE_SECONDS: f64 = {integration['staleIntegratingSeconds']}.0;",
        f'pub const PROJECT_BRAIN_DOCUMENT_NAMESPACE: &str = "{project_brain["physicalNamespace"]}";',
        f'pub const PROJECT_BRAIN_CHECKPOINT_NAMESPACE: &str = "{project_brain["checkpointNamespace"]}";',
        f'pub const PROJECT_BRAIN_EVENT_INDEX_NAMESPACE: &str = "{project_brain["eventIndexNamespace"]}";',
        f'pub const PROJECT_BRAIN_UPDATED_INDEX_NAMESPACE: &str = "{project_brain["updatedIndexNamespace"]}";',
        f'pub const PROJECT_BRAIN_RECOVERY_NAMESPACE: &str = "{project_brain["recoverySummaryNamespace"]}";',
        "pub const MAX_PROJECT_BRAIN_PROJECT_KEY_CHARACTERS: usize = "
        f"{project_brain['maximumProjectKeyCharacters']};",
        "pub const MAX_PROJECT_BRAIN_NARRATIVE_TEXT_BYTES: usize = "
        f"{project_brain['maximumNarrativeTextBytes']};",
        "pub const PROJECT_BRAIN_EVENT_CHECKPOINT_THRESHOLD: u64 = "
        f"{project_brain['eventCheckpointThreshold']};",
        "pub const PROJECT_BRAIN_EVENT_CHECKPOINT_TAIL: u64 = "
        f"{project_brain['eventCheckpointTail']};",
        f'pub const SCHEDULER_TASK_DOCUMENT_NAMESPACE: &str = "{scheduler["physicalNamespace"]}";',
        f'pub const SCHEDULER_TASK_ID_CLAIM_NAMESPACE: &str = "{scheduler["tenantGlobalIdClaimNamespace"]}";',
        f'pub const SCHEDULER_TASK_SYSTEM_KEY_CLAIM_NAMESPACE: &str = "{scheduler["systemKeyClaimNamespace"]}";',
        f'pub const SCHEDULER_TASK_EXECUTION_INDEX_NAMESPACE: &str = "{scheduler["executionIndexNamespace"]}";',
        f'pub const SCHEDULER_TASK_CREATED_INDEX_NAMESPACE: &str = "{scheduler["createdIndexNamespace"]}";',
        "pub const SCHEDULER_TASK_ENABLED_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{scheduler["enabledCreatedIndexNamespace"]}";',
        "pub const SCHEDULER_TASK_GLOBAL_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{scheduler["tenantGlobalCreatedIndexNamespace"]}";',
        "pub const SCHEDULER_TASK_GLOBAL_ENABLED_CREATED_INDEX_NAMESPACE: &str =\n"
        f'    "{scheduler["tenantGlobalEnabledCreatedIndexNamespace"]}";',
        f'pub const SCHEDULER_TASK_COUNT_NAMESPACE: &str = "{scheduler["ownerCountNamespace"]}";',
        f'pub const SCHEDULER_POLL_DOCUMENT_NAMESPACE: &str = "{scheduler["pollDocumentNamespace"]}";',
        f'pub const SCHEDULER_POLL_TIME_INDEX_NAMESPACE: &str = "{scheduler["pollTimeIndexNamespace"]}";',
        f'pub const SCHEDULER_POLL_SEQUENCE_NAMESPACE: &str = "{scheduler["pollSequenceNamespace"]}";',
        "pub const MAX_SCHEDULER_TASK_DOCUMENT_BYTES: usize = "
        f"{scheduler['maximumTaskDocumentBytes']};",
        "pub const MAX_SCHEDULER_TASK_ID_CHARACTERS: usize = "
        f"{scheduler['maximumTaskIdCharacters']};",
        "pub const MAX_SCHEDULER_SYSTEM_KEY_CHARACTERS: usize = "
        f"{scheduler['maximumSystemKeyCharacters']};",
        "pub const MAX_SCHEDULER_POLL_ROWS: usize = "
        f"{scheduler['maximumPollRows']};",
        "pub const MAX_SCHEDULER_POLL_DOCUMENT_BYTES: usize = "
        f"{scheduler['maximumPollDocumentBytes']};",
        f'pub const QUEUE_ITEM_CORE_NAMESPACE: &str = "{queue["coreNamespace"]}";',
        f'pub const QUEUE_ITEM_STATE_NAMESPACE: &str = "{queue["stateNamespace"]}";',
        f'pub const QUEUE_ITEM_ID_CLAIM_NAMESPACE: &str = "{queue["tenantGlobalIdClaimNamespace"]}";',
        f'pub const QUEUE_CONVERSATION_ORDER_INDEX_NAMESPACE: &str = "{queue["ownerConversationOrderIndexNamespace"]}";',
        f'pub const QUEUE_GLOBAL_CONVERSATION_INDEX_NAMESPACE: &str = "{queue["tenantGlobalConversationIndexNamespace"]}";',
        f'pub const QUEUE_GLOBAL_LEASE_INDEX_NAMESPACE: &str = "{queue["tenantGlobalLeaseIndexNamespace"]}";',
        f'pub const QUEUE_AUTOPILOT_MARKER_NAMESPACE: &str = "{queue["autopilotMarkerNamespace"]}";',
        f'pub const QUEUE_GLOBAL_AUTOPILOT_INDEX_NAMESPACE: &str = "{queue["tenantGlobalAutopilotIndexNamespace"]}";',
        f"pub const MAX_QUEUE_REAP_ITEMS: usize = {queue['maximumReapItems']};",
        f"pub const DEFAULT_QUEUE_PAGE_BYTES: usize = {queue['defaultPageBytes']};",
        f"pub const MAX_QUEUE_ID_CHARACTERS: usize = {queue['maximumIdCharacters']};",
        f"pub const MAX_QUEUE_CONVERSATION_ID_CHARACTERS: usize = {queue['maximumConversationIdCharacters']};",
        f'pub const TIMER_DOCUMENT_NAMESPACE: &str = "{timers["physicalNamespace"]}";',
        f'pub const TIMER_ID_CLAIM_NAMESPACE: &str = "{timers["tenantGlobalIdClaimNamespace"]}";',
        f'pub const TIMER_STATUS_CREATED_INDEX_NAMESPACE: &str = "{timers["ownerStatusCreatedIndexNamespace"]}";',
        f'pub const TIMER_CONVERSATION_INDEX_NAMESPACE: &str = "{timers["ownerConversationIndexNamespace"]}";',
        f'pub const TIMER_GLOBAL_ACTIVE_CREATED_INDEX_NAMESPACE: &str = "{timers["tenantGlobalActiveCreatedIndexNamespace"]}";',
        f'pub const TIMER_TOTAL_COUNT_NAMESPACE: &str = "{timers["ownerTotalCountNamespace"]}";',
        f'pub const TIMER_ACTIVE_COUNT_NAMESPACE: &str = "{timers["ownerActiveCountNamespace"]}";',
        f'pub const TIMER_POLL_DOCUMENT_NAMESPACE: &str = "{timers["pollDocumentNamespace"]}";',
        f'pub const TIMER_POLL_TIME_INDEX_NAMESPACE: &str = "{timers["pollTimeIndexNamespace"]}";',
        f'pub const TIMER_POLL_ID_CLAIM_NAMESPACE: &str = "{timers["pollIdClaimNamespace"]}";',
        f'pub const TIMER_POLL_SEQUENCE_NAMESPACE: &str = "{timers["pollSequenceNamespace"]}";',
        f"pub const MAX_TIMER_DOCUMENT_BYTES: usize = {timers['maximumTimerDocumentBytes']};",
        f"pub const MAX_TIMER_ID_CHARACTERS: usize = {timers['maximumTimerIdCharacters']};",
        f"pub const MAX_TIMER_LIST_ROWS: usize = {timers['maximumTimerListRows']};",
        f"pub const MAX_TIMER_GLOBAL_ACTIVE_ROWS: usize = {timers['maximumGlobalActiveRows']};",
        f"pub const MAX_TIMER_POLL_ROWS: usize = {timers['maximumPollRows']};",
        f"pub const MAX_TIMER_POLL_DOCUMENT_BYTES: usize = {timers['maximumPollDocumentBytes']};",
        "pub const MAX_RATE_LIMIT_ENDPOINT_CHARACTERS: usize = "
        f"{rate_limits['maximumEndpointCharacters']};",
        "pub const MAX_RATE_LIMIT_CLIENT_KEY_CHARACTERS: usize = "
        f"{rate_limits['maximumClientKeyCharacters']};",
        "pub const MAX_RATE_LIMIT_EVENT_ID_CHARACTERS: usize = "
        f"{rate_limits['maximumEventIdCharacters']};",
        "pub const MAX_RATE_LIMIT_EVENTS_PER_WINDOW: u64 = "
        f"{rate_limits['maximumEventsPerWindow']};",
        "pub const MAX_RATE_LIMIT_WINDOW_SECONDS: u64 = "
        f"{rate_limits['maximumWindowSeconds']};",
        f"pub const MAX_RATE_LIMIT_PRUNE_ROWS: usize = {rate_limits['maximumPruneRows']};",
        f"pub const RATE_LIMIT_COUNTER_RADIX: usize = {rate_limits['counterRadix']};",
        f"pub const RATE_LIMIT_COUNTER_DEPTH: usize = {rate_limits['counterDepth']};",
        "pub const MAX_RATE_LIMIT_WINDOW_COUNTER_READS: usize = "
        f"{rate_limits['maximumWindowCounterReads']};",
        f'pub const RECENT_PROJECT_DOCUMENT_NAMESPACE: &str = "{recent_projects["physicalNamespace"]}";',
        f'pub const RECENT_PROJECT_COUNT_NAMESPACE: &str = "{recent_projects["countNamespace"]}";',
        "pub const MAX_RECENT_PROJECT_PATH_CHARACTERS: usize = "
        f"{recent_projects['maximumPathCharacters']};",
        f'pub const PROVIDER_DOCUMENT_NAMESPACE: &str = "{providers["physicalNamespace"]}";',
        f'pub const PROVIDER_CREATED_INDEX_NAMESPACE: &str = "{providers["createdIndexNamespace"]}";',
        f'pub const PROVIDER_COUNT_NAMESPACE: &str = "{providers["ownerCountNamespace"]}";',
        f'pub const PROVIDER_ID_CLAIM_NAMESPACE: &str = "{providers["tenantGlobalClaimNamespace"]}";',
    ]
