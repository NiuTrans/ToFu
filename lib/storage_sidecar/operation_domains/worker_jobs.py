"""Durable cross-replica worker-job operation registry."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'worker_job.get': ops.OperationSpec(
        'query', False, ops._worker_job_get),
    'worker_job.enqueue': ops.OperationSpec(
        'command', True, ops._worker_job_enqueue),
    # A claim/heartbeat is a live CAS. Receipts would replay an obsolete lease.
    'worker_job.claim_next': ops.OperationSpec(
        'command', False, ops._worker_job_claim_next),
    'worker_job.heartbeat': ops.OperationSpec(
        'command', False, ops._worker_job_heartbeat),
    'worker_job.claim_state': ops.OperationSpec(
        'query', False, ops._worker_job_claim_state),
    'worker_job.request_cancel': ops.OperationSpec(
        'command', True, ops._worker_job_request_cancel),
    'worker_job.complete': ops.OperationSpec(
        'command', True, ops._worker_job_complete),
}


__all__ = ['OPERATIONS']
