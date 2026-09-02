"""Core record, event, and rate-limit operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    "system.schema_version": ops.OperationSpec("query", False, ops._schema_version),
    # Bounded incremental_vacuum slice; idempotent page bookkeeping.
    "system.reclaim": ops.OperationSpec("command", False, ops._system_reclaim),
    "record.get": ops.OperationSpec("query", False, ops._record_get),
    "record.list": ops.OperationSpec("query", False, ops._record_list),
    "record.put": ops.OperationSpec("command", True, ops._record_put),
    "record.delete": ops.OperationSpec("command", True, ops._record_delete),
    "project.charter.get": ops.OperationSpec(
        "query", False, ops._project_charter_get
    ),
    "project.charter.put": ops.OperationSpec(
        "command", True, ops._project_charter_put
    ),
    "project.charter.delete": ops.OperationSpec(
        "command", True, ops._project_charter_delete
    ),
    "project.recent.list": ops.OperationSpec(
        "query", False, ops._project_recent_list
    ),
    "project.recent.touch": ops.OperationSpec(
        "command", True, ops._project_recent_touch
    ),
    "project.recent.clear": ops.OperationSpec(
        "command", True, ops._project_recent_clear
    ),
    # Compact task lifecycle projection.  Raw record.list over this namespace
    # can exceed the 64 MiB protocol frame before recovery sees one row.
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
    "event.inspector_summary": ops.OperationSpec(
        "query", False, ops._event_inspector_summary),
    # Age-bounded DELETE is naturally idempotent; retention must not create a
    # new permanent receipt every maintenance tick.
    "event.prune": ops.OperationSpec("command", False, ops._event_prune),
    "rate_limit.record_and_check": ops.OperationSpec(
        "command", True, ops._rate_limit_record_and_check
    ),
    "board.list": ops.OperationSpec("query", False, ops._board_list),
    "board.post": ops.OperationSpec("command", True, ops._board_post),
    # Board lifecycle actions retain ambiguous-ACK receipts, but callers mint
    # one ID per invocation so later complete↔reopen cycles and lease refreshes
    # can never replay a stale task-scoped receipt.
    "board.claim": ops.OperationSpec("command", True, ops._board_claim),
    "board.dispatch": ops.OperationSpec("command", True, ops._board_dispatch),
    "board.complete": ops.OperationSpec("command", True, ops._board_complete),
    "board.reopen": ops.OperationSpec("command", True, ops._board_reopen),
    "board.write_set": ops.OperationSpec("command", True, ops._board_write_set),
    "board.mutate": ops.OperationSpec("command", True, ops._board_mutate),
    # Offline import is idempotent by owner + per-document canonical digest;
    # permanent receipts would grow with every migration batch unnecessarily.
    "board.import_batch": ops.OperationSpec(
        "command", False, ops._board_import_batch
    ),
    "watch.list": ops.OperationSpec("query", False, ops._watch_list),
    "watch.mutate": ops.OperationSpec("command", True, ops._watch_mutate),
    "watch.edit": ops.OperationSpec("command", False, ops._watch_edit),
    "watch.status": ops.OperationSpec("command", False, ops._watch_status),
    "watch.promote": ops.OperationSpec("command", False, ops._watch_promote),
    "watch.get": ops.OperationSpec("query", False, ops._watch_get),
    "watch.response.append": ops.OperationSpec(
        "command", True, ops._watch_response_append
    ),
    "watch.import_batch": ops.OperationSpec(
        "command", False, ops._watch_import_batch
    ),
    "project.feed.append": ops.OperationSpec("command", True, ops._feed_append),
    "project.feed.list": ops.OperationSpec("query", False, ops._feed_list),
    "project.status.append": ops.OperationSpec("command", True, ops._status_append),
    "project.status.list": ops.OperationSpec("query", False, ops._status_list),
}

__all__ = ["OPERATIONS"]
