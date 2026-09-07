"""Core record, event, and rate-limit operation registrations."""

from lib.project_recent_contract import PROJECT_RELINK_STORAGE_DEADLINE_SECONDS
from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "system.schema_version": ops.OperationSpec("query", False, ops._schema_version),
    # Bounded incremental_vacuum slice; idempotent page bookkeeping.
    "system.reclaim": ops.OperationSpec("command", False, ops._system_reclaim),
    "record.get": ops.OperationSpec("query", False, ops._record_get),
    "record.list": ops.OperationSpec("query", False, ops._record_list),
    "record.put": ops.OperationSpec("command", True, ops._record_put),
    "record.delete": ops.OperationSpec("command", True, ops._record_delete),
    "project.recent.list": ops.OperationSpec(
        "query", False, ops._project_recent_list
    ),
    "project.recent.touch": ops.OperationSpec(
        "command", True, ops._project_recent_touch
    ),
    "project.recent.touch_many": ops.OperationSpec(
        "command", True, ops._project_recent_touch_many
    ),
    "project.recent.clear": ops.OperationSpec(
        "command", True, ops._project_recent_clear
    ),
    "project.relink": ops.OperationSpec(
        "command",
        True,
        ops._project_relink,
        transaction_timeout_s=PROJECT_RELINK_STORAGE_DEADLINE_SECONDS,
    ),
    # Compact task lifecycle projection.  Raw record.list over this namespace
    # can exceed the 64 MiB protocol frame before recovery sees one row.
    "task_results.replay_get": ops.OperationSpec(
        "query", False, ops._task_results_replay_get
    ),
    "task_results.summary_list": ops.OperationSpec(
        "query", False, ops._task_results_summary_list
    ),
    # Compact A/B-outcome projection — the report must never list the raw
    # task_results namespace (MiB-sized content/thinking blobs per value).
    "task_results.cost_experiment_scan": ops.OperationSpec(
        "query", False, ops._task_results_cost_experiment_scan
    ),
    # High-frequency CAS snapshot. Identical replay is resolved against the
    # authority row, so no permanent command receipt is needed.
    "task_results.checkpoint": ops.OperationSpec(
        "command", False, ops._task_results_checkpoint
    ),
    "task_results.abort": ops.OperationSpec(
        "command", False, ops._task_results_abort
    ),
    "task_results.abort_requested": ops.OperationSpec(
        "query", False, ops._task_results_abort_requested
    ),
    # Restart settlement for the task-inspection read model. Conversation
    # projections are settled independently by turn.recover.
    "task_results.recover_running": ops.OperationSpec(
        "command", False, ops._task_results_recover_running
    ),
    "event.append": ops.OperationSpec("command", False, ops._event_append),
    "event.append_batch": ops.OperationSpec("command", False, ops._event_append_batch),
    "event.list": ops.OperationSpec("query", False, ops._event_list),
    "event.latest": ops.OperationSpec("query", False, ops._event_latest),
    "event.bounds": ops.OperationSpec("query", False, ops._event_bounds),
    "event.inspector_summary": ops.OperationSpec(
        "query", False, ops._event_inspector_summary),
    # Age-bounded DELETE is naturally idempotent; retention must not create a
    # new permanent receipt every maintenance tick.
    "event.prune": ops.OperationSpec("command", False, ops._event_prune),
    "rate_limit.record_and_check": ops.OperationSpec(
        "command", True, ops._rate_limit_record_and_check
    ),
}

__all__ = ["OPERATIONS"]
