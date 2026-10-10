"""lib/proof/ — Proof-carrying deliverables: verifiable evidence bundles.

Responsibility: assemble a content-addressed proof bundle for one owned
task by reading EXISTING evidence authorities (terminal task snapshot,
per-attempt Turn Trace, Request Inspector request rounds, project file
receipts), and verify submitted bundles. This package is a read-only
assembly layer: it produces no new facts, owns no write path, and never
becomes a second authority for the evidence it reads.

Entry points:
  - ``assemble_task_proof(task_id, owner_user_id=...)`` — build one bundle;
    ``None`` when the task is not visible to that owner (foreign and missing
    stay indistinguishable, ``task_results.replay_get`` precedent).
  - ``verify_proof_bundle(bundle, resolver=...)`` — structural verification
    always; a full evidence re-check when a resolver is supplied.
  - ``lib.proof.evidence.server_evidence_resolver(owner_user_id)`` — the
    server-side resolver used by the verify route.

Dependencies: read-only readers in ``lib.tasks_pkg`` (imported lazily inside
``lib/proof/evidence.py`` / ``assemble.py`` functions so blueprint import
stays cheap and import cycles stay impossible) and the wire authority
``contracts/deliverable_proof_v1.schema.json``.

Design record: ``docs/PROOF_DELIVERABLES_DESIGN_PROPOSAL.md``.
"""
from .assemble import assemble_task_proof
from .errors import ProofError
from .verify import verify_proof_bundle

__all__ = ['assemble_task_proof', 'verify_proof_bundle', 'ProofError']
