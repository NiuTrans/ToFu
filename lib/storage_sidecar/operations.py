"""Versioned semantic operation catalog; callers never provide SQL.

Facade: handlers live in ``lib.storage_sidecar.operations_pkg`` slices and are
re-exported here so existing ``from lib.storage_sidecar import operations``
callers (registry domains, CLI, tests) keep working unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.storage.errors import StorageError
from lib.storage_sidecar.operations_pkg._common import (
    _required_text as _required_text,
    _integer as _integer,
    _number as _number,
    _expected_version as _expected_version,
    _dump as _dump,
    _load as _load,
    _wire_document as _wire_document,
    OperationSpec as OperationSpec,
    _schema_version as _schema_version,
    _system_reclaim as _system_reclaim,
)
from lib.storage_sidecar.operations_pkg._records import (
    _record_get as _record_get,
    _record_list as _record_list,
    _task_results_summary_list as _task_results_summary_list,
    _task_results_cost_experiment_scan as _task_results_cost_experiment_scan,
    _task_results_checkpoint as _task_results_checkpoint,
    _task_results_abort as _task_results_abort,
    _task_results_abort_requested as _task_results_abort_requested,
    _task_results_recover_running as _task_results_recover_running,
    _record_put as _record_put,
    _record_delete as _record_delete,
    _append_event_row as _append_event_row,
    _event_append as _event_append,
    _event_append_batch as _event_append_batch,
    _event_list as _event_list,
    _event_latest as _event_latest,
    _event_inspector_summary as _event_inspector_summary,
    _event_prune as _event_prune,
    _rate_limit_record_and_check as _rate_limit_record_and_check,
)
from lib.storage_sidecar.operations_pkg._project import (
    _project_charter_get as _project_charter_get,
    _project_charter_put as _project_charter_put,
    _project_charter_delete as _project_charter_delete,
    _project_recent_list as _project_recent_list,
    _project_recent_touch as _project_recent_touch,
    _project_recent_clear as _project_recent_clear,
)
from lib.storage_sidecar.operations_pkg._knowledge import (
    _knowledge_document_list as _knowledge_document_list,
    _knowledge_document_get as _knowledge_document_get,
    _knowledge_document_content as _knowledge_document_content,
    _knowledge_document_find_digest as _knowledge_document_find_digest,
    _knowledge_document_create as _knowledge_document_create,
    _knowledge_document_replace as _knowledge_document_replace,
    _knowledge_document_delete as _knowledge_document_delete,
    _knowledge_settings_get as _knowledge_settings_get,
    _knowledge_settings_patch as _knowledge_settings_patch,
    _knowledge_availability as _knowledge_availability,
    _knowledge_catalog as _knowledge_catalog,
    _knowledge_search_candidates as _knowledge_search_candidates,
    _knowledge_asset_get as _knowledge_asset_get,
    _knowledge_enrichment_activity as _knowledge_enrichment_activity,
    _knowledge_enrichment_owners as _knowledge_enrichment_owners,
    _knowledge_asset_claim as _knowledge_asset_claim,
    _knowledge_asset_update as _knowledge_asset_update,
    _knowledge_assets_mark_no_vision as _knowledge_assets_mark_no_vision,
    _knowledge_owner_clear as _knowledge_owner_clear,
)
from lib.storage_sidecar.operations_pkg._queue import (
    _queue_conv_id as _queue_conv_id,
    _queue_marker_document as _queue_marker_document,
    _queue_autopilot_get as _queue_autopilot_get,
    _queue_autopilot_list_all as _queue_autopilot_list_all,
    _queue_autopilot_arm as _queue_autopilot_arm,
    _queue_autopilot_clear as _queue_autopilot_clear,
    _QUEUE_KINDS as _QUEUE_KINDS,
    _queue_kind as _queue_kind,
    _queue_priority as _queue_priority,
    _queue_item as _queue_item,
    _queue_rows as _queue_rows,
    _queue_renumber as _queue_renumber,
    _queue_enqueue as _queue_enqueue,
    _queue_list as _queue_list,
    _queue_remove as _queue_remove,
    _queue_clear as _queue_clear,
    _queue_dequeue as _queue_dequeue,
    _queue_lease_release as _queue_lease_release,
    _queue_reap as _queue_reap,
    _queue_lease_bind as _queue_lease_bind,
    _queue_finalize as _queue_finalize,
    _queue_depth as _queue_depth,
    _queue_conversations_list_all as _queue_conversations_list_all,
)
from lib.storage_sidecar.operations_pkg._worker_jobs import (
    _MAX_JOB_PAYLOAD_BYTES as _MAX_JOB_PAYLOAD_BYTES,
    _WORKER_JOB_COLUMNS as _WORKER_JOB_COLUMNS,
    _worker_job_get as _worker_job_get,
    _worker_job_enqueue as _worker_job_enqueue,
    _worker_job_claim_next as _worker_job_claim_next,
    _worker_job_heartbeat as _worker_job_heartbeat,
    _worker_job_claim_state as _worker_job_claim_state,
    _worker_job_request_cancel as _worker_job_request_cancel,
    _worker_job_complete as _worker_job_complete,
)
from lib.storage_sidecar.operations_pkg._archives import (
    _archive_create as _archive_create,
    _archive_list as _archive_list,
    _archive_get as _archive_get,
    _archive_update_summary as _archive_update_summary,
    _archive_delete_conversation as _archive_delete_conversation,
    _archive_prune as _archive_prune,
)
from lib.storage_sidecar.operations_pkg._conversations import (
    _CONVERSATION_METADATA as _CONVERSATION_METADATA,
    _conversation_identity as _conversation_identity,
    _conversation_document as _conversation_document,
    _turn_actor_to_legacy_role as _turn_actor_to_legacy_role,
    _turn_to_legacy_message as _turn_to_legacy_message,
    _derive_turn_messages as _derive_turn_messages,
    _conversation_get as _conversation_get,
    _backfill_turn_message_counts as _backfill_turn_message_counts,
    _conversation_list as _conversation_list,
    _derive_turn_messages_bulk as _derive_turn_messages_bulk,
    _conversation_count as _conversation_count,
    _conversation_search_op as _conversation_search_op,
    _conversation_metadata as _conversation_metadata,
    _conversation_create as _conversation_create,
    _conversation_settings_update as _conversation_settings_update,
    _conversation_metadata_update as _conversation_metadata_update,
    _conversation_delete as _conversation_delete,
    _conversation_restore as _conversation_restore,
    _conversation_clone as _conversation_clone,
    _conversation_purge as _conversation_purge,
    _conversation_trash_prune as _conversation_trash_prune,
)
from lib.storage_sidecar.operations_pkg._runs import (
    _RUN_STATUSES as _RUN_STATUSES,
    _TERMINAL_RUN_STATUSES as _TERMINAL_RUN_STATUSES,
    _json_text as _json_text,
    _run_status as _run_status,
    _decode_run_error as _decode_run_error,
    _run_row as _run_row,
    _orchestration_run_create as _orchestration_run_create,
    _orchestration_run_get as _orchestration_run_get,
    _orchestration_run_list as _orchestration_run_list,
    _orchestration_run_update as _orchestration_run_update,
    _orchestration_run_retire as _orchestration_run_retire,
    _orchestration_run_retire_all as _orchestration_run_retire_all,
    _orchestration_event_append as _orchestration_event_append,
    _orchestration_event_project as _orchestration_event_project,
    _orchestration_event_page as _orchestration_event_page,
    _orchestration_run_delete as _orchestration_run_delete,
    _SWARM_NONTERMINAL as _SWARM_NONTERMINAL,
    _swarm_json as _swarm_json,
    _optional_text as _optional_text,
    _swarm_session_save as _swarm_session_save,
    _swarm_session_terminate as _swarm_session_terminate,
    _swarm_session_delete as _swarm_session_delete,
    _swarm_agent_save as _swarm_agent_save,
    _swarm_agents_mark_delivered as _swarm_agents_mark_delivered,
    _swarm_session_get as _swarm_session_get,
    _swarm_resumable_list as _swarm_resumable_list,
)
from lib.storage_sidecar.operations_pkg._papers import (
    _research_lang as _research_lang,
    _paper_report_upsert as _paper_report_upsert,
    _paper_report_get as _paper_report_get,
    _paper_report_latest as _paper_report_latest,
    _paper_report_second_pass_merge as _paper_report_second_pass_merge,
    _paper_report_second_pass_accumulate as _paper_report_second_pass_accumulate,
    _paper_translation_upsert as _paper_translation_upsert,
    _paper_translation_get as _paper_translation_get,
    _paper_library_put as _paper_library_put,
    _paper_library_delete as _paper_library_delete,
    _paper_library_recent as _paper_library_recent,
    _paper_library_list as _paper_library_list,
    _paper_library_identity as _paper_library_identity,
    _paper_library_title_backfill as _paper_library_title_backfill,
    _paper_note_list as _paper_note_list,
    _paper_note_create as _paper_note_create,
    _paper_note_update as _paper_note_update,
    _paper_note_delete as _paper_note_delete,
    _daily_cost_date as _daily_cost_date,
    _daily_cost_month as _daily_cost_month,
    _daily_cost_upsert as _daily_cost_upsert,
    _daily_cost_delete as _daily_cost_delete,
    _daily_cost_persisted_dates as _daily_cost_persisted_dates,
    _daily_cost_latest as _daily_cost_latest,
    _paper_podcast_key as _paper_podcast_key,
    _paper_podcast_upsert as _paper_podcast_upsert,
    _paper_podcast_get as _paper_podcast_get,
    _paper_podcast_mark_interrupted as _paper_podcast_mark_interrupted,
)
from lib.storage_sidecar.operations_pkg._tenant import (
    _TENANT_USER_ROLES as _TENANT_USER_ROLES,
    _TENANT_USER_STATUSES as _TENANT_USER_STATUSES,
    _TENANT_USER_COLUMNS as _TENANT_USER_COLUMNS,
    _tenant_user_document as _tenant_user_document,
    _tenant_user_role as _tenant_user_role,
    _tenant_user_status as _tenant_user_status,
    _tenant_user_create as _tenant_user_create,
    _tenant_user_get as _tenant_user_get,
    _tenant_user_list as _tenant_user_list,
    _tenant_user_set_status as _tenant_user_set_status,
    _tenant_user_set_role as _tenant_user_set_role,
    _tenant_user_authentication as _tenant_user_authentication,
    _tenant_user_record_login as _tenant_user_record_login,
)
from lib.storage_sidecar.operations_pkg._credentials import (
    _CREDENTIAL_COLUMNS as _CREDENTIAL_COLUMNS,
    _credential_document as _credential_document,
    _credential_create as _credential_create,
    _credential_create_if_owner_empty as _credential_create_if_owner_empty,
    _credential_list as _credential_list,
    _credential_get as _credential_get,
    _credential_authenticate as _credential_authenticate,
    _credential_validate as _credential_validate,
    _credential_touch as _credential_touch,
    _credential_identify as _credential_identify,
    _credential_update as _credential_update,
    _credential_revoke as _credential_revoke,
    _credential_exists as _credential_exists,
)
from lib.storage_sidecar.operations_pkg._providers import (
    _MAX_PROVIDERS_PER_OWNER as _MAX_PROVIDERS_PER_OWNER,
    _PROVIDER_COLUMNS as _PROVIDER_COLUMNS,
    _provider_create as _provider_create,
    _provider_delete as _provider_delete,
    _provider_document as _provider_document,
    _provider_get as _provider_get,
    _provider_list as _provider_list,
    _provider_touch as _provider_touch,
    _provider_update as _provider_update,
)
from lib.storage_sidecar.operations_pkg._orchestration_definitions import (
    _DEFINITION_COLUMNS as _DEFINITION_COLUMNS,
    _definition_document as _definition_document,
    _orchestration_definition_create as _orchestration_definition_create,
    _orchestration_definition_delete as _orchestration_definition_delete,
    _orchestration_definition_get as _orchestration_definition_get,
    _orchestration_definition_list as _orchestration_definition_list,
    _orchestration_definition_update as _orchestration_definition_update,
    _workflow_owner as _workflow_owner,
)
from lib.storage_sidecar.operations_pkg._artifacts import (
    _ARTIFACT_FORMATS as _ARTIFACT_FORMATS,
    _ARTIFACT_MAX_BYTES as _ARTIFACT_MAX_BYTES,
    _ARTIFACT_COLUMNS as _ARTIFACT_COLUMNS,
    _artifact_document as _artifact_document,
    _artifact_create as _artifact_create,
    _artifact_get as _artifact_get,
    _artifact_list as _artifact_list,
    _artifact_delete as _artifact_delete,
    _artifact_versions as _artifact_versions,
    _artifact_library as _artifact_library,
    _artifact_pin as _artifact_pin,
    _tool_result_artifact_put as _tool_result_artifact_put,
    _tool_result_artifact_read as _tool_result_artifact_read,
    _tool_result_artifact_search as _tool_result_artifact_search,
    _tool_result_artifact_prune as _tool_result_artifact_prune,
    _research_artifact_upsert as _research_artifact_upsert,
    _research_artifacts_get as _research_artifacts_get,
    _research_directions_list as _research_directions_list,
)
from lib.storage_sidecar.operations_pkg._optimizer import (
    _OPT_PROPOSAL_COLUMNS as _OPT_PROPOSAL_COLUMNS,
    _OPT_ACTION_COLUMNS as _OPT_ACTION_COLUMNS,
    _optimizer_proposal_create as _optimizer_proposal_create,
    _optimizer_proposal_update as _optimizer_proposal_update,
    _optimizer_proposal_get as _optimizer_proposal_get,
    _optimizer_proposal_list as _optimizer_proposal_list,
    _optimizer_action_record as _optimizer_action_record,
    _optimizer_action_outcome as _optimizer_action_outcome,
    _optimizer_action_revert as _optimizer_action_revert,
    _optimizer_action_list as _optimizer_action_list,
    _optimizer_action_expired as _optimizer_action_expired,
    _optimizer_action_for_proposal as _optimizer_action_for_proposal,
    _log_aggregate_flush as _log_aggregate_flush,
    _log_aggregate_query as _log_aggregate_query,
)
from lib.storage_sidecar.operations_pkg._plugins import (
    _plugin_register as _plugin_register,
    _plugin_manifest_get as _plugin_manifest_get,
    _plugin_context as _plugin_context,
    _plugin_dynamic as _plugin_dynamic,
)
from lib.storage_sidecar.operations_pkg._timers import (
    _TIMER_COLUMNS as _TIMER_COLUMNS,
    _timer_id as _timer_id,
    _timer_document as _timer_document,
    _timer_get as _timer_get,
    _timer_list as _timer_list,
    _timer_history as _timer_history,
    _timer_create as _timer_create,
    _timer_active_list_all as _timer_active_list_all,
    _timer_active_count as _timer_active_count,
    _timer_cancel as _timer_cancel,
    _timer_update as _timer_update,
    _timer_poll_append as _timer_poll_append,
    _timer_poll_commit as _timer_poll_commit,
    _timer_progress as _timer_progress,
    _timer_poll_log as _timer_poll_log,
    _SCHEDULER_COLUMNS as _SCHEDULER_COLUMNS,
    _SCHEDULER_NUMERIC as _SCHEDULER_NUMERIC,
    _scheduler_task_id as _scheduler_task_id,
    _scheduler_document as _scheduler_document,
    _scheduler_get as _scheduler_get,
    _scheduler_list as _scheduler_list,
    _scheduler_list_all as _scheduler_list_all,
    _scheduler_create as _scheduler_create,
    _scheduler_ensure as _scheduler_ensure,
    _scheduler_update as _scheduler_update,
    _scheduler_delete as _scheduler_delete,
    _scheduler_record_result as _scheduler_record_result,
    _scheduler_claim_due as _scheduler_claim_due,
    _scheduler_poll_append as _scheduler_poll_append,
    _scheduler_poll_log as _scheduler_poll_log,
)
from lib.storage_sidecar.operations_pkg._turns import (
    _TURN_CHANGE_CAPTURE_OPERATIONS as _TURN_CHANGE_CAPTURE_OPERATIONS,
    _turn_identity as _turn_identity,
    _turn_change_capture as _turn_change_capture,
    _turn_public as _turn_public,
    _attempt_public as _attempt_public,
    _turn_get as _turn_get,
    _turn_list as _turn_list,
    _turn_list_delta as _turn_list_delta,
    _turn_sync_snapshot as _turn_sync_snapshot,
    _turn_sync_changes as _turn_sync_changes,
    _turn_sync_prune as _turn_sync_prune,
    _attempt_get as _attempt_get,
    _turn_revision as _turn_revision,
    _turn_events as _turn_events,
    _SLIM_HYDRATABLE_TYPES as _SLIM_HYDRATABLE_TYPES,
    _hydrate_slim_frame_tail as _hydrate_slim_frame_tail,
    _turn_events_prune as _turn_events_prune,
    _turn_event_append as _turn_event_append,
    _turn_exists as _turn_exists,
    _turn_create_pair as _turn_create_pair,
    _turn_append_settled as _turn_append_settled,
    _turn_attempt_claim as _turn_attempt_claim,
    _turn_attempt_create as _turn_attempt_create,
    _turn_projection_update as _turn_projection_update,
    _turn_related_announce as _turn_related_announce,
    _turn_branch_create as _turn_branch_create,
    _turn_branch_delete as _turn_branch_delete,
    _turn_compact as _turn_compact,
    _turn_delete as _turn_delete,
    _turn_recover as _turn_recover,
    _turn_cleanup as _turn_cleanup,
    _turn_search_backfill as _turn_search_backfill,
    _visible_shape as _visible_shape,
    _turn_visible_sync as _turn_visible_sync,
    _turn_attempt_bind as _turn_attempt_bind,
    _turn_event_record as _turn_event_record,
)
from lib.storage_sidecar.operations_pkg._worker_dispatch import (
    _CONVERSATION_ATTEMPT_JOB_CONTRACT as _CONVERSATION_ATTEMPT_JOB_CONTRACT,
    _CONVERSATION_ATTEMPT_JOB_KIND as _CONVERSATION_ATTEMPT_JOB_KIND,
    _turn_attempt_dispatch_worker as _turn_attempt_dispatch_worker,
)
from lib.storage_sidecar.operations_pkg._board import (
    _board_public as _board_public,
    _board_list as _board_list,
    _board_post as _board_post,
    _board_claim as _board_claim,
    _board_dispatch as _board_dispatch,
    _board_complete as _board_complete,
    _board_mutate as _board_mutate,
    _board_reopen as _board_reopen,
    _board_write_set as _board_write_set,
    _watch_public as _watch_public,
    _watch_response_rows as _watch_response_rows,
    _watch_list as _watch_list,
    _watch_get as _watch_get,
    _watch_mutate as _watch_mutate,
    _watch_edit as _watch_edit,
    _watch_status as _watch_status,
    _watch_promote as _watch_promote,
    _watch_response_append as _watch_response_append,
    _board_import_batch as _board_import_batch,
    _watch_import_batch as _watch_import_batch,
)
from lib.storage_sidecar.operations_pkg._feed import (
    _feed_append as _feed_append,
    _feed_list as _feed_list,
    _status_append as _status_append,
    _status_list as _status_list,
)


_OPERATIONS: dict[str, OperationSpec] | None = None


def _operation_catalog() -> dict[str, OperationSpec]:
    global _OPERATIONS
    if _OPERATIONS is None:
        from lib.storage_sidecar.operation_registry import build_registry

        _OPERATIONS = build_registry()
    return _OPERATIONS


def resolve_operation(operation: str, kind: str, payload: Mapping[str, Any]):
    spec = _operation_catalog().get(operation)
    if spec is not None:
        if spec.kind != kind:
            raise StorageError(
                "database_protocol_error", "Storage operation kind mismatch"
            )
        def execute(session):
            result = spec.handler(session, payload)
            if spec.after is not None:
                return spec.after(session, operation, payload, result)
            return result

        return spec.receipt_required, execute
    if operation.startswith("plugin."):
        return kind == "command", lambda session: _plugin_dynamic(
            session, operation, kind, payload
        )
    raise StorageError("database_protocol_error", "Unknown storage operation")


__all__ = ["OperationSpec", "resolve_operation"]
