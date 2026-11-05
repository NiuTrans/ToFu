"""Validate records table contracts.
Entries: named validate_* functions, called in the original order by validation.load_ir. No I/O."""

def validate_browser_observations(browser_observations) -> None:
    if (
        not isinstance(browser_observations, dict)
        or browser_observations.get("ownerKey") != "owner_user_id"
        or browser_observations.get("physicalFamily") != "entity+blob"
        or browser_observations.get("physicalNamespace")
        != "browser_site_observation_documents"
        or browser_observations.get("lruNamespace")
        != "browser_site_observations_by_last_observed"
        or browser_observations.get("maximumDocumentsPerOwner") != 200
        or browser_observations.get("maximumObservationPayloadBytes") != 4096
        or browser_observations.get("maximumStoredDocumentBytes") != 8192
        or browser_observations.get("maximumExpiredPrunedPerCommand") != 64
    ):
        raise ValueError("Tofu-DB browser observations must be bounded and owner scoped")


def validate_desktop(desktop) -> None:
    if not isinstance(desktop, dict) or desktop.get("ownerKey") != "owner_user_id":
        raise ValueError("Tofu-DB desktop table must be owner scoped")


def validate_records(records) -> None:
    if (
        not isinstance(records, dict)
        or records.get("ownerKey") != "authenticated_owner_user_id"
        or records.get("taskResultPhysicalNamespace") != "task_result_documents"
        or records.get("taskResultHeaderNamespace") != "task_result_headers"
        or records.get("taskResultReplayNamespace") != "task_result_replay_projections"
        or records.get("taskResultSummaryNamespace") != "task_results_by_owner"
        or records.get("taskResultLiveNamespace") != "task_results_live_by_owner"
        or records.get("taskResultCostExperimentNamespace")
        != "task_results_by_cost_experiment"
        or records.get("taskResultMaximumBytes") != 64 * 1024 * 1024
        or records.get("taskResultMaximumSummaryRows") != 1000
        or records.get("taskResultMaximumSummaryScanRows") != 10_000
        or records.get("taskResultSummaryPageRows") != 1000
        or records.get("taskResultMaximumRecoveryRows") != 500
        or records.get("taskResultMaximumRecoveryScanRows") != 100_000
        or records.get("taskResultMaximumCostExperimentRows") != 10_000
        or records.get("taskResultMaximumCostExperimentScanRows") != 256
        or records.get("taskResultCheckpointGuardContract")
        != "tofu.task-results.checkpoint.guard/v1"
        or records.get("taskResultCacheSettingsContract")
        != "tofu.task-results.checkpoint.cache-settings/v1"
        or records.get("taskResultToolEffectAdmission") != {
            "requestField": "tool_effect_intent", "responseField": "tool_effect_intent",
            "type": "string", "minLength": 1, "requiresGuardedParent": True,
            "workerScope": "registered-descendant", "revalidateOnReplay": True,
        }
        or records.get("taskResultCacheFactMaximum") != 2_147_483_647
    ):
        raise ValueError("Tofu-DB records table must be owner scoped")


def validate_daily_costs(daily_costs) -> None:
    if (
        not isinstance(daily_costs, dict)
        or daily_costs.get("ownerKey") != "authenticated_owner_user_id"
        or daily_costs.get("physicalFamily") != "entity+blob"
        or daily_costs.get("physicalNamespace") != "daily_cost_documents"
        or daily_costs.get("countNamespace") != "daily_cost_counts"
        or daily_costs.get("documentAdmission") != "blob-backed-without-private-byte-quota"
        or daily_costs.get("queryAdmission") != "complete-owner-scoped-month-and-date-probes-without-private-quota"
    ):
        raise ValueError("Tofu-DB daily costs must preserve complete owner-scoped admission")


def validate_log_aggregates(log_aggregates) -> None:
    if (
        not isinstance(log_aggregates, dict)
        or log_aggregates.get("ownerKey") != "authenticated_tenant_and_owner"
        or log_aggregates.get("physicalFamily") != "entity+blob"
        or log_aggregates.get("physicalNamespace") != "log_aggregate_documents"
        or log_aggregates.get("sweepIndexNamespace") != "log_aggregates_by_last_seen"
        or log_aggregates.get("sequenceNamespace") != "log_aggregate_sequences"
        or log_aggregates.get("maximumFingerprintCharacters") != 64
        or log_aggregates.get("maximumLevelCharacters") != 32
        or log_aggregates.get("maximumLoggerCharacters") != 256
        or log_aggregates.get("maximumTemplateCharacters") != 200
        or log_aggregates.get("maximumSampleCharacters") != 2000
        or log_aggregates.get("maximumDocumentBytes") != 16384
        or log_aggregates.get("maximumFlushBatch") != 500
        or log_aggregates.get("maximumQueryRows") != 500
        or log_aggregates.get("maximumScanRows") != 4096
        or log_aggregates.get("maximumSweepBatch") != 500
        or log_aggregates.get("maximumRowCount") != 1_000_000_000
    ):
        raise ValueError("Tofu-DB log aggregates must be bounded tenant-global state")


def validate_optimizer_proposals(optimizer_proposals) -> None:
    if (
        not isinstance(optimizer_proposals, dict)
        or optimizer_proposals.get("ownerKey") != "authenticated_tenant_and_owner"
        or optimizer_proposals.get("physicalFamily") != "entity+blob"
        or optimizer_proposals.get("physicalNamespace") != "optimizer_proposal_documents"
        or optimizer_proposals.get("createdIndexNamespace") != "optimizer_proposals_by_created_desc"
        or optimizer_proposals.get("statusCreatedIndexNamespace") != "optimizer_proposals_by_status_created_desc"
        or optimizer_proposals.get("statusNamespace") != "optimizer_proposal_status"
        or optimizer_proposals.get("countNamespace") != "optimizer_proposal_counts"
        or optimizer_proposals.get("rowAdmission") != "owner-scoped-demand-without-count-quota"
        or optimizer_proposals.get("defaultListRows") != 50
        or optimizer_proposals.get("documentAdmission") != "blob-backed-without-private-byte-quota"
        or optimizer_proposals.get("responseAdmission") != "complete-requested-rows-without-private-byte-quota"
    ):
        raise ValueError("Tofu-DB optimizer proposals must preserve owner-scoped demand admission")


def validate_optimizer_actions(optimizer_actions) -> None:
    if (
        not isinstance(optimizer_actions, dict)
        or optimizer_actions.get("ownerKey") != "authenticated_tenant_and_owner"
        or optimizer_actions.get("physicalFamily") != "entity+blob"
        or optimizer_actions.get("physicalNamespace") != "optimizer_action_documents"
        or optimizer_actions.get("appliedIndexNamespace") != "optimizer_actions_by_applied_desc"
        or optimizer_actions.get("activeAppliedIndexNamespace") != "optimizer_active_actions_by_applied_desc"
        or optimizer_actions.get("proposalIndexNamespace") != "optimizer_actions_by_proposal_applied_desc"
        or optimizer_actions.get("expiryIndexNamespace") != "optimizer_actions_by_expiry"
        or optimizer_actions.get("countNamespace") != "optimizer_action_counts"
        or optimizer_actions.get("rowAdmission") != "owner-scoped-demand-without-count-quota"
        or optimizer_actions.get("defaultListRows") != 50
        or optimizer_actions.get("documentAdmission") != "blob-backed-without-private-byte-quota"
        or optimizer_actions.get("responseAdmission") != "complete-requested-rows-without-private-byte-quota"
    ):
        raise ValueError("Tofu-DB optimizer actions must preserve owner-scoped demand admission")


def validate_rate_limits(rate_limits) -> None:
    if (
        not isinstance(rate_limits, dict)
        or rate_limits.get("ownerKey") != "authenticated_tenant_and_owner"
        or rate_limits.get("physicalFamily") != "entity"
        or rate_limits.get("bucketNamespace") != "rate_limit_buckets"
        or rate_limits.get("eventNamespace") != "rate_limit_events"
        or rate_limits.get("expiryIndexNamespace") != "rate_limit_events_by_expiry"
        or rate_limits.get("countNamespace") != "rate_limit_radix_counts"
        or rate_limits.get("maximumEndpointCharacters") != 256
        or rate_limits.get("maximumClientKeyCharacters") != 512
        or rate_limits.get("maximumEventIdCharacters") != 200
        or rate_limits.get("maximumEventsPerWindow") != 1_000_000
        or rate_limits.get("maximumWindowSeconds") != 7 * 24 * 60 * 60
        or rate_limits.get("maximumPruneRows") != 256
        or rate_limits.get("counterRadix") != 256
        or rate_limits.get("counterDepth") != 8
        or rate_limits.get("maximumWindowCounterReads") != 8 * 255
    ):
        raise ValueError("Tofu-DB rate limits must be bounded and owner scoped")


def validate_artifacts(artifacts, tool_result_artifacts) -> None:
    if (
        not isinstance(artifacts, dict)
        or artifacts.get("ownerKey") != "authenticated_owner_user_id"
        or artifacts.get("contentPolicy") != "complete-content-without-byte-quota"
        or "maximumContentBytes" in artifacts
        or artifacts.get("countPolicy") != "complete-conversation-count-without-admission-quota"
        or "maximumRowsPerConversation" in artifacts
        or not isinstance(tool_result_artifacts, dict)
        or tool_result_artifacts.get("ownerKey") != "user_id"
        or tool_result_artifacts.get("maximumContentBytes") != 16 * 1024 * 1024
        or tool_result_artifacts.get("maximumRangeBytes") != 64 * 1024
        or tool_result_artifacts.get("maximumTtlMilliseconds")
        != 7 * 24 * 60 * 60 * 1000
        or tool_result_artifacts.get("maximumPruneRows") != 5000
    ):
        raise ValueError("Tofu-DB artifacts must be bounded and owner scoped")
