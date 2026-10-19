"""Owner-scoped retained paper bytes; natural-key retries and bounded GC.

prepare/append/seal use the immutable build + upload witness; leases use a
caller-owned token. Chunk reads require a live owner/build lease. Discard cannot
remove a document pin. Research workspace CAS and chat document generations
own publication of their respective pins.
"""
from lib.storage_authority_sql.operations_pkg import _paper_artifacts as handlers
from lib.storage_authority_sql.operations_pkg._common import OperationSpec

OPERATIONS = {
    'paper.artifact.prepare': OperationSpec('command', False, handlers._paper_artifact_prepare),
    'paper.artifact.append': OperationSpec('command', False, handlers._paper_artifact_append),
    'paper.artifact.seal': OperationSpec('command', False, handlers._paper_artifact_seal, transaction_timeout_s=30),
    'paper.artifact.discard': OperationSpec('command', False, handlers._paper_artifact_discard),
    'paper.artifact.lease': OperationSpec('command', False, handlers._paper_artifact_lease),
    'paper.artifact.read': OperationSpec('query', False, handlers._paper_artifact_read),
    'paper.artifact.latest': OperationSpec('query', False, handlers._paper_artifact_latest),
    'paper.artifact.prune': OperationSpec('command', False, handlers._paper_artifact_prune),
}
